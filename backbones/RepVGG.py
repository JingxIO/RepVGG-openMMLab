from mim.utils import exit_with_error

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    from mmcls.models.builder import BACKBONES
except ImportError:
    exit_with_error('Please install mmcls, mmcv, torch to run this example.')


class SEBlock(nn.Module):
    """Squeeze Excitation Block
    the “Squeeze-and-Excitation” (SE) block, that adaptively recalibrates channel-wise 
    feature responses by explicitly modelling interdependencies between channels.
    
    Args:
        input_channels: down/up sampling channels
        internal_neurons: internal sampling channels
    """
    def __init__(self, input_channels, internal_neurons):
        super(SEBlock, self).__init__()
        self.down = nn.Conv2d(in_channels=input_channels,
                              out_channels=internal_neurons,
                              kernel_size=1,
                              stride=1,
                              bias=True)
        self.up = nn.Conv2d(in_channels=internal_neurons,
                            out_channels=input_channels,
                            kernel_size=1,
                            stride=1,
                            bias=True)
        self.input_channels = input_channels

    def forward(self, inputs):
        x = F.avg_pool2d(inputs, kernel_size=inputs.size(3))
        x = self.down(x)
        x = F.relu(x)
        x = self.up(x)
        x = torch.sigmoid(x)
        x = x.view(-1, self.input_channels, 1, 1)
        return inputs * x


def conv_bn(in_channels, out_channels, kernel_size, stride, padding, groups=1):

    result = nn.Sequential()
    result.add_module(
        'conv',
        nn.Conv2d(in_channels=in_channels,
                  out_channels=out_channels,
                  kernel_size=kernel_size,
                  stride=stride,
                  padding=padding,
                  groups=groups,
                  bias=False))
    result.add_module('bn', nn.BatchNorm2d(num_features=out_channels))

    return result


class RepVGGBlock(nn.Module):
    """RepVGG BLock Module
    Args:
        in_channels: input channels
        out_channels: output channels
        kernel_size: kernel size 
        use_se: use SEBlock or not
    """
    def __init__(self,
                 in_channels,
                 out_channels,
                 kernel_size,
                 stride=1,
                 padding=0,
                 dilation=1,
                 groups=1,
                 padding_mode='zeros',
                 use_se=False):

        super(RepVGGBlock, self).__init__()
        assert kernel_size == 3, "kernel size only 33 or 11"
        assert padding == 1, "this padding to keep the input feature map \
                                        size equal to the output size"

        padding11 = padding - kernel_size // 2

        if use_se:
            self.se = SEBlock(out_channels,
                              internal_neurons=out_channels // 16)
        else:
            self.se = nn.Identity()

        self.nonlinearity = nn.ReLU()

        self.rbr_identity = nn.BatchNorm2d(num_features=in_channels) \
                            if out_channels == in_channels and stride == 1 else None

        self.rbr_dense = conv_bn(in_channels=in_channels,
                                 out_channels=out_channels,
                                 kernel_size=kernel_size,
                                 stride=stride,
                                 padding=padding,
                                 groups=groups)
        self.rbr_1x1 = conv_bn(in_channels=in_channels,
                               out_channels=out_channels,
                               kernel_size=1,
                               stride=stride,
                               padding=padding11,
                               groups=groups)

    def forward(self, inputs):
        if self.rbr_identity is None:
            id_out = 0
        else:
            id_out = self.rbr_identity(inputs)

        return self.nonlinearity(
            self.se(self.rbr_dense(inputs) + self.rbr_1x1(inputs) + id_out))


@BACKBONES.register_module()
class RepVGG(nn.Module):
    """VGG backbone
    Example:
        model = RepVGG(numclasses = 1000,
                        num_blocks=[4, 6, 16, 1],
                        width_multiplier=[2.5, 2.5, 2.5, 5],
                        override_groups_map=g4_map)
        use model..
        
    Args:
        num_blocks: Depth of RepVGG, from [4, 6, 16, 1] .
        width_multiplier : stage width  ,from [2.5, 2.5, 2.5, 5] ,default None
        override_groups_map:.... ,default None
        use_se: use SEBlock or not ,default False
    """
    def __init__(self,
                 num_blocks,
                 num_classes,
                 use_se=False,
                 width_multiplier=None,
                 override_groups_map=None):

        super(RepVGG, self).__init__()
        assert len(width_multiplier) == 4, " "
        self.override_groups_map = override_groups_map or dict()
        assert 0 not in self.override_groups_map, " "

        self.use_se = use_se
        self.cur_layer_idx = 1
        self.in_planes = min(64, int(64 * width_multiplier[0]))
        self.stage0 = RepVGGBlock(in_channels=3,
                                  out_channels=self.in_planes,
                                  kernel_size=3,
                                  stride=2,
                                  padding=1,
                                  use_se=self.use_se)

        self.stage1 = self._make_stage(int(64 * width_multiplier[0]),
                                       num_blocks[0],
                                       stride=2)
        self.stage2 = self._make_stage(int(128 * width_multiplier[1]),
                                       num_blocks[1],
                                       stride=2)
        self.stage3 = self._make_stage(int(256 * width_multiplier[2]),
                                       num_blocks[2],
                                       stride=2)
        self.stage4 = self._make_stage(int(512 * width_multiplier[3]),
                                       num_blocks[3],
                                       stride=2)
        self.gap = nn.AdaptiveAvgPool2d(output_size=1)
        self.linear = nn.Linear(int(512 * width_multiplier[3]), num_classes)

    def _make_stage(self, planes, num_blocks, stride):
        strides = [stride] + [1] * (num_blocks - 1)
        blocks = []
        for stride in strides:
            cur_groups = self.override_groups_map.get(self.cur_layer_idx, 1)
            blocks.append(
                RepVGGBlock(in_channels=self.in_planes,
                            out_channels=planes,
                            kernel_size=3,
                            stride=stride,
                            padding=1,
                            groups=cur_groups,
                            use_se=self.use_se))
            self.in_planes = planes
            self.cur_layer_idx += 1
        return nn.Sequential(*blocks)

    def forward(self, x):
        assert x.shape[1] == 3, "first input channel equal 3"
        out = self.stage0(x)
        out = self.stage1(out)
        out = self.stage2(out)
        out = self.stage3(out)
        out = self.stage4(out)
        out = self.gap(out)
        out = out.view(out.size(0), -1)
        out = self.linear(out)
        return out
