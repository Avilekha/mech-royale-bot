# Build from this folder: docker build -t mech-royale-bot .
# The match runs with no network, a read-only root (writable /tmp), 1 CPU, 512 MB, and the map at /arena.
FROM python:3.13-slim
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/survival_bot.py"]
