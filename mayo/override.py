from functools import partial
from collections import Sequence, namedtuple

import numpy as np
import tensorflow as tf

from mayo.log import log
from mayo.util import Percent


def _is_constant(*args):
    return all(isinstance(a, (bool, int, float)) for a in args)


def _is_numpy(*args):
    if _is_constant(*args):
        return False
    return all(isinstance(a, (bool, int, float, np.ndarray)) for a in args)


def _is_tensor(*args):
    return any(isinstance(a, tf.Tensor) for a in args)


def _cast(value, dtype):
    if _is_constant(value):
        return dtype(value)
    if _is_numpy(value):
        dtypes = {
            float: np.float32,
            int: np.int32,
        }
        return np.cast[dtypes[dtype]](value)
    dtypes = {
        float: tf.float32,
        int: tf.int32,
    }
    return tf.cast(value, dtypes[dtype])


def _sum(value):
    if _is_constant(value):
        raise TypeError
    if _is_numpy(value):
        return np.sum(value)
    return tf.reduce_sum(value)


def _count(value):
    if _is_constant(value):
        raise TypeError
    if _is_numpy(value):
        return value.size
    return value.shape.num_elements()


def _round(value):
    if _is_constant(value):
        return round(value)
    if _is_numpy(value):
        return np.round(value)
    omap = {'Round': 'Identity'}
    with tf.get_default_graph().gradient_override_map(omap):
        return tf.round(value)


def _abs(value):
    if _is_constant(value):
        return abs(value)
    if _is_numpy(value):
        return np.abs(value)
    return tf.abs(value)


def _binary_bool_operation(a, b, op):
    if _is_constant(a, b):
        raise TypeError('Element-wise operator not supported on scalars.')
    if _is_numpy(a, b):
        return getattr(np, op)(a, b)
    return getattr(tf, op)(a, b)


_logical_or = partial(_binary_bool_operation, op='logical_or')
_logical_and = partial(_binary_bool_operation, op='logical_and')


def _clip(*args, min_max=None):
    if _is_constant(*args):
        return min(*args) if min_max else max(*args)
    if _is_numpy(*args):
        return np.min(*args) if min_max else np.max(*args)
    return tf.minimum(*args) if min_max else tf.maximum(*args)


_min = partial(_clip, min_max=True)
_max = partial(_clip, min_max=False)


def _binarize(tensor, threshold):
    return _cast(_abs(tensor) > threshold, float)


def _clip_by_value(tensor, minimum, maximum, transparent_backprop=False):
    if _is_tensor(tensor, minimum, maximum):
        return _min(_max(tensor, minimum), maximum)
    omap = {}
    if transparent_backprop:
        omap = {'Minimum': 'Identity', 'Maximum': 'Identity'}
    with tf.get_default_graph().gradient_override_map(omap):
        return tf.clip_by_value(tensor, minimum, maximum)


class OverrideNotAppliedError(Exception):
    """Invoke apply before update.  """


class BaseOverrider(object):
    """
    Base class for applying overriding operations on a Net.  Please ensure
    both methods `_apply` and `_update` are overridden with appropriate
    implementations.

    The method `_apply` overrides the variable in `value`, returns the
    overridden result; `_update` updates states of tensorflow variables used in
    `_apply`.
    """
    def __init__(self, should_update=True):
        super().__init__()
        self.name = None
        self.internals = {}
        self.should_update = should_update

    def _apply(self, getter, value):
        """
        Override this method called in `.apply()` to modify the
        variable in `value`.
        """
        raise NotImplementedError(
            'Overrider method "apply" must be implemented.')

    def apply(self, getter, value):
        """
        Things to apply to the variable in `value`, returns the
        overridden result.
        """
        def tracking_getter(name, *args, **kwargs):
            var = getter(name, *args, **kwargs)
            self.internals[name] = var
            return var
        self._applied = True
        self.name = value.op.name
        self.before = value
        self.after = self._apply(tracking_getter, value)
        return self.after

    def _update(self, session):
        """
        Override this method called in `.update()` to update internal
        states of the overrider.
        """
        pass

    def update(self, session):
        """Update things to apply during training.  """
        if not self.should_update:
            return
        if not getattr(self, '_applied', False):
            raise OverrideNotAppliedError(
                'Method "apply" must be invoked before call "update".')
        self._update(session)
        log.debug('Updated overrider {!r}'.format(self.info(session)))

    def assign(self, session):
        """Assign overridden values to parameters before overriding.  """
        session.run(tf.assign(self._before, self._after))

    def reset(self, session):
        """Reset internal variables to their respective initial values.  """
        for var in self.internals.values():
            session.run(tf.assign(var, var.initial_value))

    def _info_tuple(self, **kwargs):
        # relies on dict ordering
        cls = self.__class__.__name__
        cls_name = '{}Info'.format(cls)
        Tuple = namedtuple(cls_name, [cls] + list(kwargs))
        kwargs[cls] = self.name
        return Tuple(**kwargs)

    def info(self, session):
        return self._info_tuple()

    @classmethod
    def finalize_info(cls, table):
        pass

    def __repr__(self):
        if not self.name:
            return super().__repr__()
        return '<{} overrides {!r}>'.format(
            self.__class__.__qualname__, self.name)


class ChainOverrider(BaseOverrider, Sequence):
    def __init__(self, overriders, should_update=True):
        super().__init__(should_update)
        self._overriders = overriders

    def __getitem__(self, index):
        return self._overriders[index]

    def __len__(self):
        return len(self._overriders)

    def _apply(self, getter, value):
        for o in self._overriders:
            value = o.apply(getter, value)
        return value

    def _update(self, session):
        for o in self._overriders:
            o.update(session)

    def reset(self, session):
        for o in self._overriders:
            o.reset(session)

    def info(self, session):
        return self._info_tuple(overriders=self._overriders)

    def __repr__(self):
        return repr(self._overriders)


class ThresholdBinarizer(BaseOverrider):
    def __init__(self, threshold):
        super().__init__()
        self.threshold = threshold

    def _apply(self, _, value):
        return _binarize(value, self.threshold)


class BasePruner(BaseOverrider):
    def _apply(self, getter, value):
        shape = value.get_shape()
        name = '{}/mask'.format(value.op.name)
        self._mask = getter(
            name, dtype=tf.bool, shape=shape,
            initializer=tf.ones_initializer(), trainable=False)
        mask = _cast(self._mask, float)
        return value * mask

    def _updated_mask(self, var, mask, session):
        raise NotImplementedError(
            'Method to compute an updated mask is not implemented.')

    def _update(self, session):
        mask = self._updated_mask(self.before, self._mask, session)
        return session.run(tf.assign(self._mask, mask))

    def info(self, session):
        mask = _cast(session.run(self._mask), int)
        density = Percent(_sum(mask) / _count(mask))
        return self._info_tuple(
            mask=self._mask.name, density=density, count_=mask.size)

    @classmethod
    def finalize_info(cls, table):
        densities = table.get_column('density')
        count = table.get_column('count_')
        avg_density = sum(d * c for d, c in zip(densities, count)) / sum(count)
        table.set_footer([None, '    overall: ', Percent(avg_density), None])


class ThresholdPruner(BasePruner):
    def __init__(self, threshold, should_update=True):
        super().__init__(should_update)
        self.threshold = threshold

    def _updated_mask(self, var, mask, session):
        return _binarize(var, self.threshold)


class MeanStdPruner(BasePruner):
    def __init__(self, alpha, should_update=True):
        super().__init__(should_update)
        self.alpha = alpha

    def _threshold(self, tensor):
        # axes = list(range(len(tensor.get_shape()) - 1))
        axes = list(range(len(tensor.get_shape())))
        mean, var = tf.nn.moments(_abs(tensor), axes=axes)
        return mean + self.alpha * tf.sqrt(var)

    def _updated_mask(self, var, mask, session):
        return _binarize(var, self._threshold(var))


class DynamicNetworkSurgeryPruner(MeanStdPruner):
    """
    References:
        1. https://github.com/yiwenguo/Dynamic-Network-Surgery
        2. https://arxiv.org/abs/1608.04493
    """
    def __init__(
            self, c_rate, on_factor=1.1, off_factor=0.9, should_update=True):
        super().__init__(c_rate, should_update)
        self.on_factor = on_factor
        self.off_factor = off_factor

    def _updated_mask(self, var, mask, session):
        threshold = self._threshold(var)
        on_mask = _abs(var) > self.on_factor * threshold
        mask = _logical_or(mask, on_mask)
        off_mask = _abs(var) > self.off_factor * threshold
        return _logical_and(mask, off_mask)


DNSPruner = DynamicNetworkSurgeryPruner


class Mayo_DNSPruner(DynamicNetworkSurgeryPruner):
    def __init__(
            self, c_rate, on_factor=1.1, off_factor=0.9, should_update=True):
        super().__init__(c_rate, on_factor, off_factor, should_update)

    def _updated_mask(self, var, mask, session):
        return super()._updated_mask(var, mask, session)

    def _set_up_scale(self, session):
        self.scale = session.config.model.layers.scale

    def _threshold_update(self):
        self.alpha += self.scale
        log.info('inside overrider, alpha is {}'.format(self.alpha))

    def _scale_update(self, update_factor):
        self.scale = self.scale * update_factor




MayoDNSPruner = Mayo_DNSPruner


class Rounder(BaseOverrider):
    @staticmethod
    def _apply(getter, value):
        return _round(value)


class FixedPointQuantizer(BaseOverrider):
    """
    Quantize inputs into 2's compliment n-bit fixed-point values with d-bit
    dynamic range.

    Args:
        - width:
            The number of bits to use in number representation.
            If not specified, we do not limit the range of values.
        - point:
            The position of the binary point.

    References:
        [1] https://arxiv.org/pdf/1604.03168
    """
    def __init__(self, point, width=None, should_update=True):
        super().__init__(should_update)
        self.point = point
        self.width = width
        if width is not None and width < 1:
            raise ValueError(
                'Width of quantized value must be greater than 0.')

    def _quantize(
            self, value, point, compute_overflow_rate=False):
        # x << point
        shift = _cast(2 ** point, float)
        value = value * shift
        # quantize
        value = _round(value)
        # ensure number is representable without overflow
        max_value = _cast(2 ** (self.width - 1), float)
        if compute_overflow_rate:
            overflows = _logical_or(value < -max_value, value > max_value - 1)
            return _sum(_cast(overflows, int)) / _count(overflows)
        value = _clip_by_value(value, -max_value, max_value - 1)
        # revert bit-shift earlier
        return value / shift

    def _apply(self, getter, value):
        return self._quantize(value, self.point)

    def info(self, session):
        p = self.point
        if isinstance(p, tf.Variable):
            p = int(session.run(p))
        return self._info_tuple(width=self.width, point=p)


class DynamicFixedPointQuantizer(FixedPointQuantizer):
    """
    Quantize inputs into 2's compliment n-bit fixed-point values with d-bit
    dynamic range.

    Args:
        - width:
            The number of bits to use in number representation.
        - overflow_rate:
            The percentage of tolerable overflow in a certain overridden
            variable.  The method `._update_policy()` uses this information
            to compute a corresponding binary point using an update policy
            described in [1].

    References:
        [1] https://arxiv.org/pdf/1412.7024
    """
    _attempts = 100

    def __init__(self, width, overflow_rate, should_update=True):
        super().__init__(None, width, should_update=should_update)
        self.overflow_rate = overflow_rate
        self._init_point = width - 1

    def _apply(self, getter, value):
        if self.point is None:
            name = '{}/point'.format(value.op.name)
            self.point = getter(
                name, dtype=tf.int32, shape=[],
                initializer=tf.constant_initializer(self._init_point),
                trainable=False)
        return self._quantize(value, self.point)

    def _update_policy(self, tensor):
        p = self._init_point
        rate = self._quantize(tensor, p, compute_overflow_rate=True)
        if rate > self.overflow_rate:
            p -= 1
        elif 2 * rate <= self.overflow_rate:
            p += 1
        return p

    def _update(self, session):
        p = self._update_policy(session.run(self.before))
        session.run(tf.assign(self.point, p))
