# NeMo Voice Agent — local deployment (RTX PRO 5000 Blackwell)

The real `VoiceSessionProvider` behind the `agentic_os.interaction` seams: NVIDIA's
[NeMo Voice Agent](https://github.com/NVIDIA-NeMo/labs-Voice-Agent) running **fully local** —
streaming ASR + diarization + LLM + TTS over one WebSocket, no cloud keys. This directory records the
exact bring-up used on the `proxmox` GPU box so it's reproducible.

## Pinned refs (verified 2026-09-05, dependency spike)

| Component | Repo / image | Pin |
|---|---|---|
| NeMo Voice Agent | `NVIDIA-NeMo/labs-Voice-Agent` | commit `99cf08c6737537b7987f55e512a0ac8b2ce1f3e1` (no tags) |
| LLM | `nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4` via `vllm/vllm-openai:latest` | NVFP4 runs **native on Blackwell** |
| ASR | `nvidia/parakeet_realtime_eou_120m-v1` | streaming + end-of-utterance |
| Diarization | `nvidia/diar_streaming_sortformer_4spk-v2.1` | ≤4 speakers |
| TTS | `hexgrad/Kokoro-82M` | Apache-2.0 |
| NeMo-Speech.cpp (optional, lighter) | `NVIDIA/NeMo-Speech.cpp` | tag `v0.1.0` |

Blackwell note: driver ≥ the 595 line / CUDA 13 (the box runs 595.58.03 / CUDA 13.2). Use the vLLM
container (NVFP4 native) and the repo's `cu13` wheels — **not** a default CPU/cu121 wheel (missing
sm_120 kernels → "no kernel image available").

## Topology (this box)

```
host :8765  NeMo Voice Agent WebSocket (server.py)  ── ASR+diar+TTS on GPU 5000
                     │  OpenAI chat  → http://localhost:31800/v1  (thinking disabled)
                     ▼
k3s ml-services/nemo-llm  vLLM Nemotron NVFP4  ── NodePort 31800 → :8000, GPU 5000 (uuid d25f5101)
```

## GPU offload (reversible)

The 48 GB RTX PRO 5000 was shared by two k3s deployments; both were scaled to 0 to free it **for now**
(the user asked to offload whatever runs there). Restore when done:

```sh
kubectl -n ml-services scale deploy qwen-reasoning-long --replicas=1   # 37.5 GB vLLM reasoning
kubectl -n ml-services scale deploy vibevoice          --replicas=1    # 5.7 GB TTS
```

## Bring-up

```sh
# 1. LLM (k3s Deployment + NodePort 31800, pinned to the 5000 by UUID)
kubectl apply -f nemo-llm.k8s.yaml
# wait for it: curl -s http://localhost:31800/v1/models   → {"data":[{"id":"nemotron"...}]}

# 2. Voice agent (host venv — apt npm/nodejs skipped; only the server is needed)
cd /main/nemo && git clone https://github.com/NVIDIA-NeMo/labs-Voice-Agent
cd labs-Voice-Agent && git checkout 99cf08c6737537b7987f55e512a0ac8b2ce1f3e1
sudo apt-get install -y build-essential python3-dev      # cdifflib needs Python.h
uv sync                                                   # builds .venv (torch cu13 + NeMo + Pipecat)
uv run python -c "import nltk; nltk.download('cmudict'); nltk.download('averaged_perceptron_tagger_eng')"

# 3. Point the agent's LLM at our vLLM + use the served name, disable thinking for latency
#    edit examples/generic_voice_agent/server/server_configs/
#      default.yaml            → llm.model: "nemotron"     (enable_reasoning: false)
#      llm_configs/nemotron_3.5_lightning.yaml → base_url: "http://localhost:31800/v1"

# 4. Run the WebSocket server on the 5000
cd examples/generic_voice_agent/server
CUDA_VISIBLE_DEVICES=GPU-d25f5101-05b0-0f66-5e5d-a159b3f7fca1 uv run python server.py
#   → "Starting websocket server on 0.0.0.0:8765"; ASR/diar/LLM/TTS pipeline ready
```

## Verify

- LLM: `curl -s localhost:31800/v1/chat/completions -d '{"model":"nemotron","messages":[{"role":"user","content":"2+2?"}],"max_tokens":3000}'` → `"4"`.
- Voice agent: `ss -ltn | grep 8765` listening; a WS connect logs `Pipecat Client connected`.
- A full audio turn needs a **Pipecat-protocol** client (the repo's `client/` web app, or a protobuf WS
  client). The ReDevOps `NvidiaNeMoVoiceAgentProvider` (a `VoiceSessionProvider`, `agentic_os.interaction`)
  speaks that protocol and surfaces transcripts as `InteractionEvent`s → Missions — the next slice.

## Evaluation (the 328 scenarios)

`agentic_os/interaction/nemo_eval.py` exports the repo's `nemo_voice_agent/evaluation/data/{eva_airline,
tau2_airline,tau2_retail,tau2_telecom}` (328) to the harness JSONL. The repo's own
`evaluation/run_evaluation.py` is the two-bot, LLM-judged run against this live agent.
