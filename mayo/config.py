import re
import os
import ast
import copy
import glob
import operator
import collections

import yaml


def _unique(items):
    found = set([])
    keep = []
    for item in items:
        if item in found:
            continue
        found.add(item)
        keep.append(item)
    return keep


class YamlTag(object):
    tag = None

    @classmethod
    def register(cls):
        yaml.add_constructor(cls.tag, cls.constructor)
        yaml.add_representer(cls, cls.representer)

    @classmethod
    def constructor(cls, loader, node):
        raise NotImplementedError

    @classmethod
    def representer(cls, dumper, data):
        raise NotImplementedError


class YamlScalarTag(YamlTag):
    def __init__(self, content):
        super().__init__()
        self.content = content

    @classmethod
    def constructor(cls, loader, node):
        content = loader.construct_scalar(node)
        return cls(content)

    @classmethod
    def representer(cls, dumper, tag):
        return dumper.represent_scalar(cls.tag, tag.content)

    def value(self):
        raise NotImplementedError

    def __repr__(self):
        return repr('{} {!r}'.format(self.tag, self.content))


class ArithTag(YamlScalarTag):
    tag = '!arith'
    _eval_expr_map = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.BitXor: operator.xor,
        ast.USub: operator.neg,
    }

    def value(self):
        tree = ast.parse(self.content, mode='eval').body
        return self._eval(tree)

    def _eval(self, n):
        if isinstance(n, ast.Num):
            return n.n
        if isinstance(n, ast.Call):
            op = __builtins__[n.func.id]
            args = (self._eval(a) for a in n.args)
            return op(*args)
        if not isinstance(n, (ast.UnaryOp, ast.BinOp)):
            raise TypeError('Unrecognized operator node {}'.format(n))
        op = self._eval_expr_map[type(n.op)]
        if isinstance(n, ast.UnaryOp):
            return op(self._eval(n.operand))
        return op(self._eval(n.left), self._eval(n.right))


class ExecTag(YamlScalarTag):
    tag = '!exec'

    def value(self):
        try:
            return self._value
        except AttributeError:
            pass
        variables = {}
        exec(self.content, variables)
        self._value = variables
        return variables


class EvalTag(YamlScalarTag):
    tag = '!eval'

    def value(self):
        try:
            return self._value
        except AttributeError:
            pass
        import tensorflow as tf
        import mayo
        self._value = eval(self.content, {'tf': tf, 'mayo': mayo})
        return self._value


ArithTag.register()
ExecTag.register()


def _dot_path(keyable, dot_path_key, insert_if_not_exists=False):
    *dot_path, final_key = dot_path_key.split('.')
    for key in dot_path:
        if isinstance(keyable, (tuple, list)):
            key = int(key)
        if insert_if_not_exists:
            keyable = keyable.setdefault(key, keyable.__class__())
            continue
        try:
            value = keyable[key]
        except KeyError:
            raise KeyError('Key path {!r} not found.'.format(dot_path_key))
        except AttributeError:
            raise AttributeError(
                'Key path {!r} resolution stopped at {!r} because the '
                'current object is not dict-like.'.format(dot_path_key, key))
        keyable = value
    return keyable, final_key


def _dict_merge(d, md):
    for k, v in md.items():
        can_merge = k in d and isinstance(d[k], dict)
        can_merge = can_merge and isinstance(md[k], collections.Mapping)
        if can_merge:
            _dict_merge(d[k], md[k])
        else:
            d[k] = md[k]


class _DotDict(dict):
    def __init__(self, data):
        super().__init__(data)

    def _recursive_apply(self, obj, func_map):
        if isinstance(obj, dict):
            for k, v in obj.items():
                obj[k] = self._recursive_apply(v, func_map)
        elif isinstance(obj, (tuple, list, set, frozenset)):
            obj = obj.__class__(
                [self._recursive_apply(v, func_map) for v in obj])
        for cls, func in func_map.items():
            if isinstance(obj, cls):
                return func(obj)
        return obj

    def _wrap(self, obj):
        def wrap(obj):
            if type(obj) is dict:
                return _DotDict(obj)
            return obj
        return self._recursive_apply(obj, {dict: wrap})

    def _link(self, obj):
        def link_str(string):
            regex = r'\$\(([_a-zA-Z][_a-zA-Z0-9\.]+)\)'
            keys = re.findall(regex, string, re.MULTILINE)
            for k in keys:
                d, fk = _dot_path(obj, k)
                string = string.replace('$({})'.format(k), str(d[fk]))
            return string

        def link_tag(tag):
            tag = tag.__class__(link_str(tag.content))
            if isinstance(tag, ArithTag):
                return tag.value()
            return tag

        link_map = {
            str: lambda s: yaml.load(link_str(s)),
            YamlScalarTag: link_tag,
        }
        return self._recursive_apply(obj, link_map)

    def __getitem__(self, key):
        obj, key = _dot_path(self, key)
        return super(_DotDict, obj).__getitem__(key)

    def __setitem__(self, key, value):
        obj, key = _dot_path(self, key)
        return super(_DotDict, obj).__setitem__(key, value)

    def __delitem__(self, key):
        obj, key = _dot_path(self, key)
        return super(_DotDict, obj).__delitem__(key)

    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__


class Config(_DotDict):
    def __init__(self, yaml_files, overrides=None):
        self._setup_excepthook()
        unified = {}
        dictionary = {}
        self._init_system_config(unified, dictionary)
        for path in yaml_files:
            with open(path, 'r') as file:
                d = yaml.load(file)
            _dict_merge(unified, copy.deepcopy(d))
            _dict_merge(dictionary, d)
        # init search paths
        self._init_search_paths(unified, dictionary, yaml_files)
        # finalize
        self._override(dictionary, overrides)
        self._link(dictionary)
        self._wrap(dictionary)
        super().__init__(dictionary)
        self._override(unified, overrides)
        self._link(unified)
        self.unified = unified
        self._setup_log_level()

    def _init_system_config(self, unified, dictionary):
        root = os.path.dirname(__file__)
        system_yaml = os.path.join(root, 'system.yaml')
        with open(system_yaml, 'r') as file:
            system = yaml.load(file)
        _dict_merge(unified, copy.deepcopy(system))
        _dict_merge(dictionary, system)

    def _init_search_paths(self, unified, dictionary, yaml_files):
        for d in (dictionary, unified):
            search_paths = d['system']['search_paths']
            keys = [
                'datasets', 'summaries',
                'checkpoints.load', 'checkpoints.save']
            for k in keys:
                curr_paths, final_key = _dot_path(search_paths, k)
                paths = curr_paths[final_key]
                if isinstance(paths, str):
                    paths = (p.strip() for p in ';'.split(paths))
                curr_paths[final_key] = _unique(paths)
        # update defaults
        default_datasets = [os.path.dirname(f) for f in yaml_files]
        search_paths['datasets'] = default_datasets + search_paths['datasets']

    @staticmethod
    def _override(dictionary, overrides):
        if not overrides:
            return
        for override in overrides:
            k_path, v = (o.strip() for o in override.split('='))
            sub_dictionary, k = _dot_path(dictionary, k_path, True)
            sub_dictionary[k] = yaml.load(v)

    def to_yaml(self, file=None):
        if file is not None:
            file = open(file, 'w')
        kwargs = {'explicit_start': True, 'width': 70, 'indent': 4}
        return yaml.dump(self.unified, file, **kwargs)

    def image_shape(self):
        params = self.dataset.preprocess.shape
        return (params.height, params.width, params.channels)

    def label_offset(self):
        bg = self.dataset.background_class
        return int(bg.use) - int(bg.has)

    def num_classes(self):
        return self.dataset.num_classes + self.label_offset()

    def data_files(self, mode):
        try:
            path = self.dataset.path[mode]
        except KeyError:
            raise KeyError('Mode {} not recognized.'.format(mode))
        files = []
        search_paths = self.system.search_paths.datasets
        for directory in search_paths:
            files += glob.glob(os.path.join(directory, path))
        if not files:
            msg = 'No files found for dataset {} with mode {} at {!r}'
            paths = '{{{}}}/{}'.format(','.join(search_paths), path)
            raise FileNotFoundError(msg.format(self.name, mode, paths))
        return files

    def _excepthook(self, etype, evalue, etb):
        if isinstance(etype, KeyboardInterrupt):
            return
        from IPython.core import ultratb
        try:
            use_pdb = self['system.use_pdb']
        except KeyError:
            use_pdb = True
        return ultratb.FormattedTB(call_pdb=use_pdb)(etype, evalue, etb)

    def _setup_excepthook(self):
        import sys
        sys.excepthook = self._excepthook

    def _setup_log_level(self):
        level = self.system.log_level
        if level != 'info':
            from mayo.log import log
            log.level = level.mayo
        os.environ['TF_CPP_MIN_LOG_LEVEL'] = str(level.tensorflow)
