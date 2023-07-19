import json
import os
from typing import Optional

import torch
from loguru import logger
from peft import PeftModel
from tqdm import tqdm
from transformers import (
    AutoConfig,
    AutoModel,
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
)
from transformers.utils.versions import require_version

# A global registry for all model adapters
model_adapters = []


def register_model_adapter(cls):
    """Register a model adapter."""
    model_adapters.append(cls())


def get_model_adapter(model_name: str):
    """Get a model adapter for a model_name."""
    for adapter in model_adapters:
        if adapter.match(model_name):
            return adapter
    raise ValueError(f"No valid model adapter for {model_name}")


class BaseModelAdapter:
    """The base and the default model adapter."""

    def match(self, model_name):
        return True

    def load_model(self, model_name_or_path: Optional[str] = None, adapter_model: Optional[str] = None, **kwargs):
        """ Load model through transformers. """
        model_name_or_path = self.default_model_name_or_path if model_name_or_path is None else model_name_or_path
        tokenizer_kwargs = self.tokenizer_kwargs
        tokenizer_kwargs.update()

        if adapter_model is not None:
            try:
                tokenizer = self.tokenizer_class.from_pretrained(
                    adapter_model,
                    use_fast=self.tokenizer_kwargs.get("use_fast", False),
                    trust_remote_code=True,
                )
            except OSError:
                tokenizer = self.tokenizer_class.from_pretrained(
                    model_name_or_path,
                    use_fast=self.tokenizer_kwargs.get("use_fast", False),
                    trust_remote_code=True,
                )
        else:
            tokenizer = self.tokenizer_class.from_pretrained(
                model_name_or_path,
                use_fast=self.tokenizer_kwargs.get("use_fast", False),
                trust_remote_code=True,
            )

        config_kwargs = self.model_kwargs
        device = kwargs.get("device", "cuda")
        num_gpus = kwargs.get("num_gpus", 1)
        if device == "cuda":
            if "torch_dtype" not in config_kwargs:
                config_kwargs["torch_dtype"] = torch.float16
            if num_gpus != 1:
                config_kwargs["device_map"] = "auto"
                # model_kwargs["device_map"] = "sequential"  # This is important for not the same VRAM sizes

        # Quantization configurations (using bitsandbytes library).
        if kwargs.get("load_in_8bit", False):
            require_version("bitsandbytes>=0.37.0", "To fix: pip install bitsandbytes>=0.37.0")

            config_kwargs["load_in_8bit"] = True
            config_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_8bit=True,
                llm_int8_threshold=6.0,
            )
            config_kwargs["device_map"] = "auto" if device == "cuda" else None

            logger.info("Quantizing model to 8 bit.")

        elif kwargs.get("load_in_4bit", False):
            require_version("bitsandbytes>=0.39.0", "To fix: pip install bitsandbytes>=0.39.0")
            require_version("peft>=0.4.0.dev0", "To fix: pip install git+https://github.com/huggingface/peft.git")

            config_kwargs["load_in_4bit"] = True
            config_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_compute_dtype=torch.float16,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
            )
            config_kwargs["device_map"] = "auto" if device == "cuda" else None

            logger.info("Quantizing model to 4 bit.")

        config = AutoConfig.from_pretrained(model_name_or_path, trust_remote_code=True)

        use_ptuning_v2 = kwargs.get("use_ptuning_v2", False)
        if use_ptuning_v2 and adapter_model:
            prefix_encoder_file = open(f'{adapter_model}/config.json', 'r')
            prefix_encoder_config = json.loads(prefix_encoder_file.read())
            prefix_encoder_file.close()

            config.pre_seq_len = prefix_encoder_config['pre_seq_len']
            config.prefix_projection = prefix_encoder_config['prefix_projection']

        # Load and prepare pretrained models (without valuehead).
        model = self.model_class.from_pretrained(
            model_name_or_path,
            config=config,
            trust_remote_code=True,
            **config_kwargs
        )

        if hasattr(model, "ntk_inv_freq"):
            logger.info("Apply NTK scaling")

        if device == "cpu":
            model = model.float()

        # post process for special tokens
        tokenizer = self.post_tokenizer(tokenizer)
        is_chatglm = "chatglm" in str(type(model))
        is_baichuan = "baichuan" in str(type(model))

        if adapter_model is not None:
            model = self.load_adapter_model(model, tokenizer, adapter_model, is_chatglm, config_kwargs, **kwargs)

        if is_chatglm or is_baichuan:
            quantize = kwargs.get("quantize", None)
            if quantize and quantize != 16:
                logger.info(f"Quantizing model to {quantize} bit.")
                model = model.quantize(quantize)

        if device == "cuda" and num_gpus == 1 and "device_map" not in config_kwargs:
            model.to(device)

        # inference mode
        model.eval()

        return model, tokenizer

    def load_lora_model(self, model, adapter_model, model_kwargs):
        return PeftModel.from_pretrained(
            model,
            adapter_model,
            torch_dtype=model_kwargs.get("torch_dtype", torch.float16),
        )

    def load_adapter_model(self, model, tokenizer, adapter_model, is_chatglm, model_kwargs, **kwargs):
        use_ptuning_v2 = kwargs.get("use_ptuning_v2", False)
        if not is_chatglm and adapter_model:
            model_vocab_size = model.get_input_embeddings().weight.size(0)
            tokenzier_vocab_size = len(tokenizer)
            logger.info(f"Vocab of the base model: {model_vocab_size}")
            logger.info(f"Vocab of the tokenizer: {tokenzier_vocab_size}")

            if model_vocab_size != tokenzier_vocab_size:
                assert tokenzier_vocab_size > model_vocab_size
                logger.info("Resize model embeddings to fit tokenizer")
                model.resize_token_embeddings(tokenzier_vocab_size)

        if use_ptuning_v2:
            prefix_state_dict = torch.load(os.path.join(adapter_model, "pytorch_model.bin"))
            new_prefix_state_dict = {}
            for k, v in prefix_state_dict.items():
                if k.startswith("transformer.prefix_encoder."):
                    new_prefix_state_dict[k[len("transformer.prefix_encoder."):]] = v
            model.transformer.prefix_encoder.load_state_dict(new_prefix_state_dict)
            model.transformer.prefix_encoder.float()
        else:
            model = self.load_lora_model(model, adapter_model, model_kwargs)

        return model

    def post_tokenizer(self, tokenizer):
        return tokenizer

    @property
    def model_class(self):
        return AutoModelForCausalLM

    @property
    def model_kwargs(self):
        return {}

    @property
    def tokenizer_class(self):
        return AutoTokenizer

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": False}

    @property
    def default_model_name_or_path(self):
        return "zpn/llama-7b"


def load_model(
    model_name: str,
    model_name_or_path: Optional[str] = None,
    adapter_model: Optional[str] = None,
    quantize: Optional[int] = 16,
    device: Optional[str] = "cuda",
    load_in_8bit: Optional[bool] = False,
    **kwargs
):
    model_name = model_name.lower()

    if "tiger" in model_name:
        def skip(*args, **kwargs):
            pass

        torch.nn.init.kaiming_uniform_ = skip
        torch.nn.init.uniform_ = skip
        torch.nn.init.normal_ = skip

    adapter = get_model_adapter(model_name)
    model, tokenizer = adapter.load_model(
        model_name_or_path,
        adapter_model,
        device=device,
        quantize=quantize,
        load_in_8bit=load_in_8bit,
        **kwargs
    )
    return model, tokenizer


class ChatglmModelAdapter(BaseModelAdapter):
    """ https://github.com/THUDM/ChatGLM-6B """

    def match(self, model_name):
        return "chatglm" in model_name

    @property
    def model_class(self):
        return AutoModel

    @property
    def default_model_name_or_path(self):
        return "THUDM/chatglm-6b"


class LlamaModelAdapter(BaseModelAdapter):
    """ https://github.com/project-baize/baize-chatbot """

    def match(self, model_name):
        return "alpaca" in model_name or "baize" in model_name or "openbuddy-llama" in model_name or \
            "ziya-llama" in model_name.lower()

    def post_tokenizer(self, tokenizer):
        tokenizer.bos_token = "<s>"
        tokenizer.eos_token = "</s>"
        tokenizer.unk_token = "<unk>"
        return tokenizer

    @property
    def model_kwargs(self):
        return {"low_cpu_mem_usage": True}


class MossModelAdapter(BaseModelAdapter):
    """ https://github.com/OpenLMLab/MOSS """

    def match(self, model_name):
        return "moss" in model_name

    @property
    def default_model_name_or_path(self):
        return "fnlp/moss-moon-003-sft-int4"


class PhoenixModelAdapter(BaseModelAdapter):
    """ https://github.com/FreedomIntelligence/LLMZoo """

    def match(self, model_name):
        return "phoenix" in model_name

    @property
    def model_kwargs(self):
        return {"low_cpu_mem_usage": True}

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": True}

    @property
    def default_model_name_or_path(self):
        return "FreedomIntelligence/phoenix-inst-chat-7b"


class FireflyModelAdapter(BaseModelAdapter):
    """ https://github.com/yangjianxin1/Firefly """

    def match(self, model_name):
        return "firefly" in model_name

    @property
    def model_kwargs(self):
        return {"torch_dtype": torch.float32}

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": True}

    @property
    def default_model_name_or_path(self):
        return "YeungNLP/firefly-2b6"


class YuLanChatModelAdapter(BaseModelAdapter):
    """ https://github.com/RUC-GSAI/YuLan-Chat """

    def match(self, model_name):
        return "yulan" in model_name

    def post_tokenizer(self, tokenizer):
        tokenizer.bos_token = "<s>"
        tokenizer.eos_token = "</s>"
        tokenizer.unk_token = "<unk>"
        return tokenizer

    @property
    def model_kwargs(self):
        return {"low_cpu_mem_usage": True}

    def load_adapter_model(self, model, tokenizer, adapter_model, is_chatglm, model_kwargs, **kwargs):
        adapter_model = AutoModelForCausalLM.from_pretrained(
            adapter_model, torch_dtype=torch.float16, low_cpu_mem_usage=True
        )
        if model.model.embed_tokens.weight.size(0) + 1 == adapter_model.model.embed_tokens.weight.size(0):
            model.resize_token_embeddings(len(tokenizer))
            model.model.embed_tokens.weight.data[-1, :] = 0

        logger.info("Applying the delta")
        for name, param in tqdm(model.state_dict().items(), desc="Applying delta"):
            assert name in model.state_dict()
            param.data += model.state_dict()[name]

        return model


class TigerBotModelAdapter(BaseModelAdapter):
    """ https://github.com/TigerResearch/TigerBot """

    def match(self, model_name):
        return "tiger" in model_name

    @property
    def tokenizer_kwargs(self):
        return {"use_fast": True}

    @property
    def default_model_name_or_path(self):
        return "TigerResearch/tigerbot-7b-sft"


class OpenBuddyFalconModelAdapter(BaseModelAdapter):
    """ https://github.com/OpenBuddy/OpenBuddy """

    def match(self, model_name):
        return "openbuddy-falcon" in model_name

    @property
    def default_model_name_or_path(self):
        return "OpenBuddy/openbuddy-falcon-7b-v5-fp16"


class AnimaModelAdapter(LlamaModelAdapter):

    def match(self, model_name):
        return "anima" in model_name

    def load_lora_model(self, model, adapter_model, model_kwargs):
        return PeftModel.from_pretrained(model, adapter_model)


class BaiChuanModelAdapter(BaseModelAdapter):
    """ https://github.com/baichuan-inc/Baichuan-13B """

    def match(self, model_name):
        return "baichuan" in model_name

    def load_lora_model(self, model, adapter_model, model_kwargs):
        return PeftModel.from_pretrained(model, adapter_model)

    @property
    def default_model_name_or_path(self):
        return "baichuan-inc/baichuan-7B"


class GuanacoModelAdapter(LlamaModelAdapter):

    def match(self, model_name):
        return "guanaco" in model_name


class InternLMModelAdapter(BaseModelAdapter):
    """ https://github.com/InternLM/InternLM """

    def match(self, model_name):
        return "internlm" in model_name

    @property
    def default_model_name_or_path(self):
        return "internlm/internlm-chat-7b"


class StarCodeModelAdapter(BaseModelAdapter):
    """ https://github.com/bigcode-project/starcoder """

    def match(self, model_name):
        return "starcode" in model_name or "starchat" in model_name

    @property
    def tokenizer_kwargs(self):
        return {}

    @property
    def default_model_name_or_path(self):
        return "HuggingFaceH4/starchat-beta"


class AquilaModelAdapter(BaseModelAdapter):
    """ https://github.com/FlagAI-Open/FlagAI """

    def match(self, model_name):
        return "Aquila" in model_name

    @property
    def default_model_name_or_path(self):
        return "BAAI/AquilaChat-7B"


register_model_adapter(ChatglmModelAdapter)
register_model_adapter(LlamaModelAdapter)
register_model_adapter(MossModelAdapter)
register_model_adapter(PhoenixModelAdapter)
register_model_adapter(FireflyModelAdapter)
register_model_adapter(YuLanChatModelAdapter)
register_model_adapter(TigerBotModelAdapter)
register_model_adapter(OpenBuddyFalconModelAdapter)
register_model_adapter(AnimaModelAdapter)
register_model_adapter(BaiChuanModelAdapter)
register_model_adapter(GuanacoModelAdapter)
register_model_adapter(InternLMModelAdapter)
register_model_adapter(AquilaModelAdapter)

register_model_adapter(BaseModelAdapter)
