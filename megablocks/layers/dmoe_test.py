import unittest

from absl.testing import parameterized
from megablocks.layers import dmoe
from megablocks.layers import moe
from megablocks.layers import test_util
from megatron.model import transformer
import numpy as np
import torch
import torch.nn.functional as F


def dmoe_module(
        batch_size,
        seq_len,
        hidden_size,
        ffn_hidden_size,
        moe_num_experts=1,
        moe_capacity_factor=1,
        moe_top_k=1):
    # Set global arguments for megatron.
    test_util.set_megatron_arguments(
        micro_batch_size=batch_size,
        hidden_size=hidden_size,
        seq_length=seq_len,
        ffn_hidden_size=ffn_hidden_size,
        moe_num_experts=moe_num_experts,
        moe_capacity_factor=moe_capacity_factor,
        moe_top_k=moe_top_k)

    def init_method_normal(std):
        def init_(tensor):
            return torch.nn.init.normal_(tensor, 0.0, std)
        return init_
    megatron_mlp = transformer.ParallelMLP(
        init_method_normal(0.1),
        init_method_normal(0.1))
    moe_mlp = moe.MoE(
        init_method_normal(0.1),
        init_method_normal(0.1))
    dmoe_mlp = dmoe.dMoE(
        init_method_normal(0.1),
        init_method_normal(0.1))
    megatron_mlp.cuda(torch.cuda.current_device()).half()
    moe_mlp.cuda(torch.cuda.current_device()).half()
    dmoe_mlp.cuda(torch.cuda.current_device()).half()

    # Set the baseline parameters to match the dMoE
    # parameters exactly.
    with torch.no_grad():
        ne, hs, fhs = moe_mlp.w1.size()
        w1 = dmoe_mlp.w1.view([ne, fhs, hs])
        moe_mlp.w1.copy_(torch.transpose(w1, 1, 2).contiguous())
        moe_mlp.w2.copy_(dmoe_mlp.w2.view([ne, fhs, hs]))
        moe_mlp.router_weight.copy_(dmoe_mlp.router_weight)
        if moe_num_experts == 1:
            w1 = moe_mlp.w1.squeeze().t().contiguous()
            megatron_mlp.dense_h_to_4h.weight.copy_(w1)
            w2 = moe_mlp.w2.squeeze().t().contiguous()
            megatron_mlp.dense_4h_to_h.weight.copy_(w2)
    return megatron_mlp, moe_mlp, dmoe_mlp

# min size: (1, 2, 128, 2, 1)
_FORWARD_TESTS = (
    (16, 1024, 512, 1, 1),
    (16, 1024, 512, 2, 1),
    (16, 1024, 512, 4, 1),
    (16, 1024, 512, 8, 1),
    (8, 2048, 512, 1, 1),
    (8, 2048, 512, 2, 1),
    (8, 2048, 512, 4, 1),
    (16, 1024, 512, 2, 2),
    (16, 1024, 512, 4, 2),
    (16, 1024, 512, 4, 4),
    (16, 1024, 512, 8, 2),
    (16, 1024, 512, 8, 4),
    (16, 1024, 512, 8, 8),
)


_DENSE_TESTS = (
    (16, 1024, 512),
    (8, 2048, 512),
)


class dMoETest(parameterized.TestCase):

    @staticmethod
    def tearDown():
        moe.clear_load_balancing_loss()

    @parameterized.parameters(*_FORWARD_TESTS)
    def testdMoE_Forward(
            self, bs, sl, hs, num_experts, top_k):
        x = torch.randn(sl, bs, hs).half().cuda()

        _, _, layer = dmoe_module(
            batch_size=bs,
            seq_len=sl,
            hidden_size=hs,
            ffn_hidden_size=hs * 2,
            moe_num_experts=num_experts,
            moe_top_k=top_k)

        out, _ = layer(x)
        self.assertSequenceEqual(out.shape, x.shape)

    @parameterized.parameters(*_FORWARD_TESTS)
    def testdMoE_ForwardBackward(
            self, bs, sl, hs, num_experts, top_k):
        x = torch.randn(sl, bs, hs).half().cuda()
        x.requires_grad_(True)

        _, _, layer = dmoe_module(
            batch_size=bs,
            seq_len=sl,
            hidden_size=hs,
            ffn_hidden_size=hs * 2,
            moe_num_experts=num_experts,
            moe_top_k=top_k)

        out, _ = layer(x)
        self.assertSequenceEqual(out.shape, x.shape)
        loss = out.sum() + moe.batched_load_balancing_loss()
        loss.backward()
        layer.zero_grad(set_to_none=True)
        x.grad = None
        moe.clear_load_balancing_loss()

    @parameterized.parameters(*_DENSE_TESTS)
    def testdMoE_ForwardVersusBaseline(self, bs, sl, hs):
        x = torch.randn(sl, bs, hs).half().cuda()

        megatron_mlp, _, dmoe_mlp = dmoe_module(
            batch_size=bs,
            seq_len=sl,
            hidden_size=hs,
            ffn_hidden_size=hs * 2)

        expected_out, _ = megatron_mlp(x)
        out, _ = dmoe_mlp(x)
        self.assertSequenceEqual(out.shape, x.shape)
        self.assertSequenceEqual(expected_out.shape, x.shape)
        self.assertTrue(allclose(out, expected_out))

    @parameterized.parameters(*_FORWARD_TESTS)
    def testdMoE_ForwardVersusMoE(
            self, bs, sl, hs, num_experts, top_k):
        x = torch.randn(sl, bs, hs).half().cuda()

        _, moe_mlp, dmoe_mlp = dmoe_module(
            batch_size=bs,
            seq_len=sl,
            hidden_size=hs,
            ffn_hidden_size=hs,
            moe_num_experts=num_experts,
            moe_capacity_factor=0)

        expected_out, _ = moe_mlp(x)
        out, _ = dmoe_mlp(x)
        self.assertSequenceEqual(out.shape, x.shape)
        self.assertSequenceEqual(expected_out.shape, x.shape)
        self.assertTrue(allclose(out, expected_out))


if __name__ == '__main__':
    unittest.main()
