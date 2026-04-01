# Glossary

Workplace shorthand, acronyms, and internal language for Suren's projects.

## Project Codenames
| Codename | Project |
|----------|---------|
| Max | AI virtual employee for Google Meet standups |
| Everperform | HR/performance SaaS (day job product) |

## Acronyms & Terms
| Term | Meaning | Context |
|------|---------|---------|
| STT | Speech-to-Text | Audio transcription via Deepgram |
| TTS | Text-to-Speech | Voice synthesis via Deepgram |
| MPS | Metal Performance Shaders | Apple Silicon M4 GPU for PyTorch |
| BaaS | Bot as a Service | Meeting BaaS — joins Google Meet |
| OBS | OBS Studio | Virtual webcam software |
| Railway | railway.app | Cloud hosting for Max's brain |
| Meeting BaaS | meetingbaas.com | Service that joins Google Meet as a bot |

## Tools & Services
| Tool | Used for |
|------|----------|
| Deepgram | STT + TTS for Max |
| HeyGen Streaming API | Realistic talking head avatar (planned) |
| Meeting BaaS | Bot joins Google Meet |
| Railway | Hosts Claude brain server |
| OBS Studio | Virtual webcam → pipes avatar video into Google Meet |
| SadTalker | Open-source talking head (evaluated, too slow) |
| MuseTalk | Lip-sync only tool (abandoned) |

## Avatar Technologies Evaluated
| Tech | Verdict |
|------|---------|
| MuseTalk V1.5 | Lip-sync only — abandoned |
| SadTalker | Head movement but ~30s latency — not real-time |
| HeyGen Streaming | Real-time, photorealistic — best option ($29/mo) |
| D-ID | Async only — 2nd best |
| Synthesia | No API, no custom face — not suitable |
| Colossyan | Enterprise video editor — not suitable |
| LivePortrait | Deleted with cleanup |
| Hallo2 | CUDA-heavy, very slow on M4 — not viable |
