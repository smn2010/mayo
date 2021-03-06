from mayo.net.tf.gate.sparse import SparseRegularizedGatedConvolutionBase


class SqueezeExciteGatedConvolution(SparseRegularizedGatedConvolutionBase):
    def activate(self, tensor):
        return self.actives() * self.gate() * super().activate(tensor)
