#!/usr/bin/env python3
"""RVC voice conversion — converts audio to Miss Cleo's voice."""
import os, sys

sys.argv = [sys.argv[0]]

RVC_DIR = "/home/om/tools/rvc"
sys.path.insert(0, RVC_DIR)
os.chdir(RVC_DIR)

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "GPU-26ea05a0-c6cf-f491-7210-a683fe498509")
os.environ["weight_root"] = os.path.join(RVC_DIR, "assets", "weights")
os.environ["index_root"] = os.path.join(RVC_DIR, "logs")
os.environ["rmvpe_root"] = os.path.join(RVC_DIR, "assets", "rmvpe")

import torch, types

class FakeHuBERT(torch.nn.Module):
    def __init__(self):
        super().__init__()
        from transformers import HubertModel
        self.model = HubertModel.from_pretrained("facebook/hubert-base-ls960")
        self.model.eval()
    def extract_features(self, source, padding_mask=None, output_layer=None):
        with torch.no_grad():
            return (self.model(source, attention_mask=padding_mask).last_hidden_state,)
    def to(self, *a, **kw): self.model = self.model.to(*a, **kw); return self
    def half(self): self.model = self.model.half(); return self
    def float(self): self.model = self.model.float(); return self
    def eval(self): self.model.eval(); return self

m1 = types.ModuleType("fairseq"); m2 = types.ModuleType("fairseq.checkpoint_utils")
m2.load_model_ensemble_and_task = lambda f, **kw: ([FakeHuBERT()], None, None)
m1.checkpoint_utils = m2
sys.modules["fairseq"] = m1; sys.modules["fairseq.checkpoint_utils"] = m2

from scipy.io import wavfile
from configs.config import Config
from infer.modules.vc.modules import VC

_vc = None

def get_vc():
    global _vc
    if _vc is None:
        config = Config(); config.device = "cuda:0"; config.is_half = True
        _vc = VC(config)
        w = os.environ["weight_root"]; os.makedirs(w, exist_ok=True)
        dst = os.path.join(w, "miss-cleo.pth")
        if not os.path.exists(dst):
            import shutil; shutil.copy2(os.path.join(RVC_DIR, "logs/miss-cleo/G_35000.pth"), dst)
        _vc.get_vc("miss-cleo.pth")
    return _vc

def convert(input_path, output_path):
    vc = get_vc()
    info, wav_opt = vc.vc_single(0, input_path, 0, None, "rmvpe", "", None, 0.0, 3, 0, 0.25, 0.33)
    if wav_opt is not None:
        wavfile.write(output_path, wav_opt[0], wav_opt[1]); return output_path
    raise RuntimeError(f"RVC failed: {info}")

inp = os.environ.get("RVC_INPUT", "")
out = os.environ.get("RVC_OUTPUT", "")
if inp and out:
    print(f"Converted: {convert(inp, out)}")
