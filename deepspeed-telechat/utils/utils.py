# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

import os
import torch
import random
import numpy as np
from transformers import set_seed, AutoTokenizer
import json
import deepspeed
from deepspeed.runtime.zero.partition_parameters import ZeroParamStatus
import re


def print_rank_0(msg, rank=0):
    if rank <= 0:
        print(msg)


def to_device(batch, device):
    output = {}
    for k, v in batch.items():
        try:
            output[k] = v.to(device)
        except:
            output[k] = v
    return output

def get_dtype_size(dtype):
    if dtype == torch.bool:
        return 1 / 8
    bit_search = re.search("[^\d](\d+)$", str(dtype))
    if bit_search is None:
        raise ValueError(f"`dtype` is not a valid dtype: {dtype}.")
    bit_size = int(bit_search.groups()[0])
    return bit_size // 8

def save_hf_format(model, tokenizer, args, sub_folder=""):
    # used to save huggingface format, so we can use it for hf.from_pretrained
    model_to_save = model.module if hasattr(model, 'module') else model
    CONFIG_NAME = "config.json"
    WEIGHTS_NAME = "pytorch_model_00001-of-00001.bin"
    output_dir = os.path.join(args.output_dir, sub_folder)
    os.makedirs(output_dir, exist_ok=True)
    output_model_file = os.path.join(output_dir, WEIGHTS_NAME)
    output_config_file = os.path.join(output_dir, CONFIG_NAME)
    save_dict = model_to_save.state_dict()
    for key in list(save_dict.keys()):
        if "lora" in key:
            del save_dict[key]
    torch.save(save_dict, output_model_file)
    model_to_save.config.to_json_file(output_config_file)
    tokenizer.save_pretrained(output_dir)
    index_dict = {"weight_map": {}, "metadata": {}}
    total_size = 0
    for name, param in model_to_save.named_parameters():
        if "lora" not in name:
            index_dict["weight_map"][name] = WEIGHTS_NAME
            total_size += param.numel() * get_dtype_size(param.dtype)
    index_dict["metadata"]["total_size"] = total_size
    with open(os.path.join(output_dir, "pytorch_model.bin.index.json"), "w", encoding="utf-8") as f:
        json_config = json.dumps(index_dict, indent=2, sort_keys=True) + "\n"
        f.write(json_config)
    os.system(f"cp -r {args.model_name_or_path}/*telechat* {output_dir}")
    os.system(f"cp -r {args.model_name_or_path}/generation* {output_dir}")

def set_random_seed(seed):
    if seed is not None:
        set_seed(seed)
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)


def get_all_reduce_mean(tensor):
    torch.distributed.all_reduce(tensor, op=torch.distributed.ReduceOp.SUM)
    tensor = tensor / torch.distributed.get_world_size()
    return tensor


def get_optimizer_grouped_parameters(model,
                                     weight_decay,
                                     no_decay_name_list=[
                                         "bias", "LayerNorm.weight"
                                     ]):
    optimizer_grouped_parameters = [
        {
            "params": [
                p for n, p in model.named_parameters()
                if (not any(nd in n
                            for nd in no_decay_name_list) and p.requires_grad)
            ],
            "weight_decay":
            weight_decay,
        },
        {
            "params": [
                p for n, p in model.named_parameters()
                if (any(nd in n
                        for nd in no_decay_name_list) and p.requires_grad)
            ],
            "weight_decay":
            0.0,
        },
    ]
    return optimizer_grouped_parameters


def _z3_params_to_fetch(param_list):
    return [
        p for p in param_list
        if hasattr(p, 'ds_id') and p.ds_status == ZeroParamStatus.NOT_AVAILABLE
    ]

def save_zero_three_model(model, tokenizer, args, sub_folder=""):
    zero_stage_3 = (args.zero_stage == 3)
    os.makedirs(args.output_dir, exist_ok=True)
    WEIGHTS_NAME = "pytorch_model_00001-of-00001.bin"
    output_dir = os.path.join(args.output_dir, sub_folder)
    os.makedirs(output_dir, exist_ok=True)
    output_model_file = os.path.join(output_dir, WEIGHTS_NAME)
    model_to_save = model.module if hasattr(model, 'module') else model

    index_dict = {"weight_map": {}, "metadata": {}}
    total_size = 0


    if not zero_stage_3:
        if args.global_rank == 0:
            torch.save(model_to_save.state_dict(), output_model_file)
            for k, v in model_to_save.named_parameters():
                if "lora" not in k:
                    index_dict["weight_map"][k] = WEIGHTS_NAME
                    total_size += v.numel() * get_dtype_size(v.dtype)
    else:
        output_state_dict = {}
        for k, v in model_to_save.named_parameters():

            if hasattr(v, 'ds_id'):
                with deepspeed.zero.GatheredParameters(_z3_params_to_fetch([v
                                                                            ]),
                                                       enabled=zero_stage_3):
                    v_p = v.data.cpu()
            else:
                v_p = v.cpu()
            if args.global_rank == 0 and "lora" not in k:
                output_state_dict[k] = v_p
                index_dict["weight_map"][k] = WEIGHTS_NAME
                total_size += v.numel() * get_dtype_size(v.dtype)
        if args.global_rank == 0:
            torch.save(output_state_dict, output_model_file)
        del output_state_dict

    if args.global_rank == 0:
        index_dict["metadata"]["total_size"] = total_size
        with open(os.path.join(output_dir, "pytorch_model.bin.index.json"), "w", encoding="utf-8") as f:
            json_config = json.dumps(index_dict, indent=2, sort_keys=True) + "\n"
            f.write(json_config)
        tokenizer.save_pretrained(output_dir)
        os.system(f"cp -r {args.model_name_or_path}/*telechat* {output_dir}")
        os.system(f"cp -r {args.model_name_or_path}/generation* {output_dir}")
