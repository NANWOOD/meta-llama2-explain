# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the Llama 2 Community License
# Agreement.

import json
import os
import sys
import time
from pathlib import Path
from typing import List, Literal, Optional, Tuple, TypedDict

import torch
import torch.nn.functional as F
from fairscale.nn.model_parallel.initialize import (
    get_model_parallel_rank,
    initialize_model_parallel,
    model_parallel_is_initialized,
)

from llama.model import ModelArgs, Transformer
from llama.tokenizer import Tokenizer

Role = Literal["system", "user", "assistant"]


class Message(TypedDict):
    """Basic Message class for chat messages."""

    role: Role
    content: str


class CompletionPrediction(TypedDict, total=False):
    """CompletionPrediction class for text completion predictions."""

    generation: str
    tokens: List[str]  # not required
    logprobs: List[float]  # not required


class ChatPrediction(TypedDict, total=False):
    """ChatPrediction class for chat completion predictions."""

    generation: Message
    tokens: List[str]  # not required
    logprobs: List[float]  # not required


Dialog = List[Message]

B_INST, E_INST = "[INST]", "[/INST]"
B_SYS, E_SYS = "<<SYS>>\n", "\n<</SYS>>\n\n"

SPECIAL_TAGS = [B_INST, E_INST, "<<SYS>>", "<</SYS>>"]
UNSAFE_ERROR = "Error: special tags are not allowed as part of the prompt."


class Llama:
    """Llama class for text generation using the language model."""

    @staticmethod
    def build(
        ckpt_dir: str,
        tokenizer_path: str,
        max_seq_len: int,
        max_batch_size: int,
        model_parallel_size: Optional[int] = None,
        seed: int = 1,
    ) -> "Llama":
        """通过初始化和加载预训练模型来构建一个 Llama 实例.

        Args:
            ckpt_dir (str): 包含 checkpoint 文件的目录的路径.
            tokenizer_path (str): 分词器文件的路径.
            max_seq_len (int): 输入文本 (prompt) 的最长序列长度.
            max_batch_size (int): 推理阶段能接受的最大 batch 大小.
            model_parallel_size (Optional[int], optional): 模型并行的进程数.
                如果未提供, 则从环境中确定. 默认值为 None.
            seed (int, optional): 随机种子，用于结果复现. 默认种子为 1.

        Returns:
            Llama: 一个加载了模型和分词器的 Llama 类实例.

        Raises:
            AssertionError: 如果指定目录中没有对应的 checkpoint 文件,
                或者模型并行大小与 checkpoint 文件数量不匹配则会报错.

        Note:
            此函数主要负责:
                1) 初始化分布式进程组.
                2) 设定进程对应的 CUDA 设备号.
                3) 加载预训练好的模型和分词器.

        """
        # 检查 Pytorch 分布式环境是否未初始化.
        if not torch.distributed.is_initialized():
            # 使用 NCCL 后端初始化.
            torch.distributed.init_process_group("nccl")

        # 检查并初始化模型并行.
        if not model_parallel_is_initialized():
            if model_parallel_size is None:
                # 从环境变量中获取 world size (理解为总进程数).
                model_parallel_size = int(os.environ.get("WORLD_SIZE", 1))
            # 初始化模型并行处理.
            initialize_model_parallel(model_parallel_size)

        # 获取当前进程的 local rank (理解为当前机器的进程编号)
        # 之所以有前缀 local 是因为在多机多卡的情况下, 每台机器的 local rank 是从 0 开始的.
        local_rank = int(os.environ.get("LOCAL_RANK", 0))

        # 设置当前进程的 CUDA 设备号.
        torch.cuda.set_device(local_rank)

        # TODO [keli]: Refer to https://github.com/meta-llama/llama/issues/1114
        # 在所有进程中设定相同的随机种子以保证实验/任务的可重复性.
        # https://pytorch.org/docs/stable/notes/randomness.html#pytorch-random-number-generator
        # 尽管有些博客中说 torch.manual_seed 只能 seed CPU, 但实际上它也会 seed GPU.
        torch.manual_seed(seed)

        # 如果不是主进程, 则屏蔽标准输出, 防止多进程输出冗余信息.
        if local_rank > 0:
            # os.devnull 是一个特殊文件, 用于丢弃输出.
            sys.stdout = open(os.devnull, "w")

        # 开始计时, 用来统计加载时间.
        start_time = time.time()

        # 加载所有 checkpoint 文件路径.
        # Q: 为什么要排序呢?
        # A: 排序是为了保证使用模型并行索引能够正确加载对应的 checkpoint 文件.
        # (前提是 checkpoint 文件名是类似于 0.pth, 1.pth 这样的格式.)
        checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))

        # 检查 checkpoint 文件是否存在.
        assert len(checkpoints) > 0, f"no checkpoint files found in {ckpt_dir}"

        # 检查 checkpoint 文件数量是否与模型并行数匹配.
        # Q: 为什么要检查模型并行数和检查点数量是否匹配? 为什么要每个进程加载一个检查点?
        # A: 注意到这里是模型并行, 一个大型的模型被分割成多个部分, 每个部分由一个进程负责.
        assert model_parallel_size == len(
            checkpoints
        ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {model_parallel_size}"

        # 获取当前进程对应的 checkpoint 文件路径.
        # (因为模型并行, 每个进程加载一个对应的 checkpoint 文件.)
        ckpt_path = checkpoints[get_model_parallel_rank()]

        # 加载 checkpoint 文件到 CPU.
        # Q: 为什么要加载到 CPU? 为什么不直接加载到 GPU?
        # A: 个人猜测是为了统一输出 device，保证输出一定是位于 CPU 上的, 兼容性更高也方便用户使用.
        # 否则你无法预期输出的 device 是什么, 有可能是当前进程的 GPU, 也有可能是其他进程的 GPU.
        checkpoint = torch.load(ckpt_path, map_location="cpu")

        # 读取模型构造相关参数.
        with open(Path(ckpt_dir) / "params.json", "r") as f:
            params = json.loads(f.read())

        # 根据参数构建模型参数对象 (包括一些 generation 时需要的参数).
        model_args: ModelArgs = ModelArgs(
            max_seq_len=max_seq_len,
            max_batch_size=max_batch_size,
            **params,
        )

        # 加载分词器.
        tokenizer = Tokenizer(model_path=tokenizer_path)

        # 更新模型参数重的词汇表大小.
        model_args.vocab_size = tokenizer.n_words

        # 设置默认张量类型为半精度浮点数 (fp16) .
        torch.set_default_tensor_type(torch.cuda.HalfTensor)

        # 创建 Transformer 模型实例.
        model = Transformer(model_args)

        # 通过 checkpoint 加载模型参数.
        # Q: 为什么要设置 strict=False?
        # A: strict=False 表示允许加载不完全匹配的模型参数, 模型的部分参数不在 checkpoint 中,
        # 但不影响加载. 出现不完全匹配的原因是因为我们可能处于模型并行的环境中.
        model.load_state_dict(checkpoint, strict=False)

        # 输出加载的时间开销.
        print(f"Loaded in {time.time() - start_time:.2f} seconds")

        # 返回构建好的 Llama 实例.
        return Llama(model, tokenizer)

    def __init__(self, model: Transformer, tokenizer: Tokenizer):
        self.model = model
        self.tokenizer = tokenizer

    @torch.inference_mode()
    def generate(
        self,
        prompt_tokens: List[List[int]],
        max_gen_len: int,
        temperature: float = 0.6,
        top_p: float = 0.9,
        logprobs: bool = False,
        echo: bool = False,
    ) -> Tuple[List[List[int]], Optional[List[List[float]]]]:
        """Generate text sequences based on provided prompts using the language generation model.

        Args:
            prompt_tokens (List[List[int]]): List of tokenized prompts, where each prompt is
                represented as a list of integers.
            max_gen_len (int): Maximum length of the generated text sequence.
            temperature (float, optional): Temperature value for controlling randomness in
                sampling. Defaults to 0.6.
            top_p (float, optional): Top-p probability threshold for nucleus sampling.
                Defaults to 0.9.
            logprobs (bool, optional): Flag indicating whether to compute token log probabilities.
                Defaults to False.
            echo (bool, optional): Flag indicating whether to include prompt tokens in the
                generated output. Defaults to False.

        Returns:
            Tuple[List[List[int]], Optional[List[List[float]]]]: A tuple containing generated
                token sequences and, if logprobs is True, corresponding token log probabilities.

        Note:
            This method uses the provided prompts as a basis for generating text. It employs
                nucleus sampling to produce text with controlled randomness.
            If logprobs is True, token log probabilities are computed for each generated token.

        """
        params = self.model.params
        bsz = len(prompt_tokens)
        assert bsz <= params.max_batch_size, (bsz, params.max_batch_size)

        min_prompt_len = min(len(t) for t in prompt_tokens)
        max_prompt_len = max(len(t) for t in prompt_tokens)
        assert max_prompt_len <= params.max_seq_len
        total_len = min(params.max_seq_len, max_gen_len + max_prompt_len)

        pad_id = self.tokenizer.pad_id
        tokens = torch.full(
            (bsz, total_len), pad_id, dtype=torch.long, device="cuda"
        )
        for k, t in enumerate(prompt_tokens):
            tokens[k, : len(t)] = torch.tensor(
                t, dtype=torch.long, device="cuda"
            )
        if logprobs:
            token_logprobs = torch.zeros_like(tokens, dtype=torch.float)

        prev_pos = 0
        eos_reached = torch.tensor([False] * bsz, device="cuda")
        input_text_mask = tokens != pad_id
        if min_prompt_len == total_len:
            logits = self.model.forward(tokens, prev_pos)
            token_logprobs = -F.cross_entropy(
                input=logits.transpose(1, 2),
                target=tokens,
                reduction="none",
                ignore_index=pad_id,
            )

        for cur_pos in range(min_prompt_len, total_len):
            logits = self.model.forward(tokens[:, prev_pos:cur_pos], prev_pos)
            if temperature > 0:
                probs = torch.softmax(logits[:, -1] / temperature, dim=-1)
                next_token = sample_top_p(probs, top_p)
            else:
                next_token = torch.argmax(logits[:, -1], dim=-1)

            next_token = next_token.reshape(-1)
            # only replace token if prompt has already been generated
            next_token = torch.where(
                input_text_mask[:, cur_pos], tokens[:, cur_pos], next_token
            )
            tokens[:, cur_pos] = next_token
            if logprobs:
                token_logprobs[:, prev_pos + 1 : cur_pos + 1] = (
                    -F.cross_entropy(
                        input=logits.transpose(1, 2),
                        target=tokens[:, prev_pos + 1 : cur_pos + 1],
                        reduction="none",
                        ignore_index=pad_id,
                    )
                )
            eos_reached |= (~input_text_mask[:, cur_pos]) & (
                next_token == self.tokenizer.eos_id
            )
            prev_pos = cur_pos
            if all(eos_reached):
                break

        if logprobs:
            token_logprobs = token_logprobs.tolist()
        out_tokens, out_logprobs = [], []
        for i, toks in enumerate(tokens.tolist()):
            # cut to max gen len
            start = 0 if echo else len(prompt_tokens[i])
            toks = toks[start : len(prompt_tokens[i]) + max_gen_len]
            probs = None
            if logprobs:
                probs = token_logprobs[i][
                    start : len(prompt_tokens[i]) + max_gen_len
                ]
            # cut to eos tok if any
            if self.tokenizer.eos_id in toks:
                eos_idx = toks.index(self.tokenizer.eos_id)
                toks = toks[:eos_idx]
                probs = probs[:eos_idx] if logprobs else None
            out_tokens.append(toks)
            out_logprobs.append(probs)
        return (out_tokens, out_logprobs if logprobs else None)

    def text_completion(
        self,
        prompts: List[str],
        temperature: float = 0.6,
        top_p: float = 0.9,
        max_gen_len: Optional[int] = None,
        logprobs: bool = False,
        echo: bool = False,
    ) -> List[CompletionPrediction]:
        """Perform text completion for a list of prompts using the language generation model.

        Args:
            prompts (List[str]): List of text prompts for completion.
            temperature (float, optional): Temperature value for controlling randomness in
                sampling. Defaults to 0.6.
            top_p (float, optional): Top-p probability threshold for nucleus sampling.
                Defaults to 0.9.
            max_gen_len (Optional[int], optional): Maximum length of the generated completion
                sequence. If not provided, it's set to the model's maximum sequence length minus 1.
            logprobs (bool, optional): Flag indicating whether to compute token log probabilities.
                Defaults to False.
            echo (bool, optional): Flag indicating whether to include prompt tokens in the
                generated output. Defaults to False.

        Returns:
            List[CompletionPrediction]: List of completion predictions, each containing the
                generated text completion.

        Note:
            This method generates text completions for the provided prompts, employing nucleus
                sampling to introduce controlled randomness.
            If logprobs is True, token log probabilities are computed for each generated token.

        """
        if max_gen_len is None:
            max_gen_len = self.model.params.max_seq_len - 1
        prompt_tokens = [
            self.tokenizer.encode(x, bos=True, eos=False) for x in prompts
        ]
        generation_tokens, generation_logprobs = self.generate(
            prompt_tokens=prompt_tokens,
            max_gen_len=max_gen_len,
            temperature=temperature,
            top_p=top_p,
            logprobs=logprobs,
            echo=echo,
        )
        if logprobs:
            return [
                {
                    "generation": self.tokenizer.decode(t),
                    "tokens": [self.tokenizer.decode(x) for x in t],
                    "logprobs": logprobs_i,
                }
                for t, logprobs_i in zip(generation_tokens, generation_logprobs)
            ]
        return [
            {"generation": self.tokenizer.decode(t)} for t in generation_tokens
        ]

    def chat_completion(
        self,
        dialogs: List[Dialog],
        temperature: float = 0.6,
        top_p: float = 0.9,
        max_gen_len: Optional[int] = None,
        logprobs: bool = False,
    ) -> List[ChatPrediction]:
        """Generate assistant responses for a list of conversational dialogs using the language
        generation model.

        Args:
            dialogs (List[Dialog]): List of conversational dialogs, where each dialog is
                a list of messages.
            temperature (float, optional): Temperature value for controlling randomness in
                sampling. Defaults to 0.6.
            top_p (float, optional): Top-p probability threshold for nucleus sampling.
                Defaults to 0.9.
            max_gen_len (Optional[int], optional): Maximum length of the generated response
                sequence. If not provided, it's set to the model's maximum sequence length minus 1.
            logprobs (bool, optional): Flag indicating whether to compute token log probabilities.
                Defaults to False.

        Returns:
            List[ChatPrediction]: List of chat predictions, each containing the assistant'
                generated response.

        Raises:
            AssertionError: If the last message in a dialog is not from the user.
            AssertionError: If the dialog roles are not in the required 'user', 'assistant', and
                optional 'system' order.

        Note:
            This method generates assistant responses for the provided conversational dialogs.
            It employs nucleus sampling to introduce controlled randomness in text generation.
            If logprobs is True, token log probabilities are computed for each generated token.

        """  # noqa: D205
        if max_gen_len is None:
            max_gen_len = self.model.params.max_seq_len - 1
        prompt_tokens = []
        unsafe_requests = []
        for dialog in dialogs:
            unsafe_requests.append(
                any(
                    [
                        tag in msg["content"]
                        for tag in SPECIAL_TAGS
                        for msg in dialog
                    ]
                )
            )
            if dialog[0]["role"] == "system":
                dialog = [
                    {
                        "role": dialog[1]["role"],
                        "content": (
                            B_SYS
                            + dialog[0]["content"]
                            + E_SYS
                            + dialog[1]["content"]
                        ),
                    }
                ] + dialog[2:]
            assert all([msg["role"] == "user" for msg in dialog[::2]]) and all(
                [msg["role"] == "assistant" for msg in dialog[1::2]]
            ), (
                "model only supports 'system', 'user' and 'assistant' roles, "
                "starting with 'system', then 'user' and alternating (u/a/u/a/u...)"
            )
            dialog_tokens: List[int] = sum(
                [
                    self.tokenizer.encode(
                        f"{B_INST} {(prompt['content']).strip()} {E_INST} {(answer['content']).strip()} ",
                        bos=True,
                        eos=True,
                    )
                    for prompt, answer in zip(
                        dialog[::2],
                        dialog[1::2],
                    )
                ],
                [],
            )
            assert (
                dialog[-1]["role"] == "user"
            ), f"Last message must be from user, got {dialog[-1]['role']}"
            dialog_tokens += self.tokenizer.encode(
                f"{B_INST} {(dialog[-1]['content']).strip()} {E_INST}",
                bos=True,
                eos=False,
            )
            prompt_tokens.append(dialog_tokens)

        generation_tokens, generation_logprobs = self.generate(
            prompt_tokens=prompt_tokens,
            max_gen_len=max_gen_len,
            temperature=temperature,
            top_p=top_p,
            logprobs=logprobs,
        )
        if logprobs:
            return [
                {
                    "generation": {
                        "role": "assistant",
                        "content": (
                            self.tokenizer.decode(t)
                            if not unsafe
                            else UNSAFE_ERROR
                        ),
                    },
                    "tokens": [self.tokenizer.decode(x) for x in t],
                    "logprobs": logprobs_i,
                }
                for t, logprobs_i, unsafe in zip(
                    generation_tokens, generation_logprobs, unsafe_requests
                )
            ]
        return [
            {
                "generation": {
                    "role": "assistant",
                    "content": (
                        self.tokenizer.decode(t) if not unsafe else UNSAFE_ERROR
                    ),
                }
            }
            for t, unsafe in zip(generation_tokens, unsafe_requests)
        ]


def sample_top_p(probs: torch.Tensor, p: float) -> torch.Tensor:
    """对概率分布进行 top-p (nucleus) 采样.

    推荐阅读: https://github.com/keli-wen/AGI-Study/blob/master/inference/Basic-LLM-Inference.md
    #top-pnucleus-sampling

    Args:
        probs (torch.Tensor): 概率分布张量. Shape: (batch_size, vocab_size).
        p (float): 用于 top-p 采样的概率阈值.

    Returns:
        torch.Tensor: 采样后的 token 索引. Shape: (batch_size, 1).

    Note:
        Top-p 采样选择的是其累积概率超过阈值 p 的最小 token 集合.
        据选定的 token 重新规范化概率分布.
    """
    # 对概率进行降序排序. 降序是因为 nucleus 是按概率从大到小选择 token 集合.
    probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)

    # 计算累积概率. 这是为了后续快速做差分然后判断 token 是否在 top-p 集合中.
    probs_sum = torch.cumsum(probs_sort, dim=-1)

    # 创建一个掩码, 排除累积概率超过阈值 p 的部分, 所以需要减去当前概率判断是否已经超过阈值.
    mask = probs_sum - probs_sort > p

    # 使用掩码将超过阈值的 tokens 概率设置为 0.
    probs_sort[mask] = 0.0

    # 对筛选后的概率重新规范化.
    # eg. [0.2, 0.2, 0.2, 0.2] /0.8 -> [0.25, 0.25, 0.25, 0.25].
    # `div_` 方法是原地操作, 将规范化后的概率分布保存在 probs_sort 中.
    probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))

    # 从规范化的后的概率分布中采样一个 token.
    # `torch.multinomial` 方法是从多项分布中采样, 返回的是采样的索引.
    next_token = torch.multinomial(probs_sort, num_samples=1)

    # 根据采样的索引找到对应的原始索引.
    next_token = torch.gather(probs_idx, -1, next_token)

    # 返回采样得到的 token 索引.
    return next_token
