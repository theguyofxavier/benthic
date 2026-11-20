# Benthic Online

A Krillion-style rare-answer trivia game. Every prompt has dozens of right answers; the rarer yours, the deeper you dive.

This repo is the online version that runs on a Raspberry Pi:

- `index.html` – the whole game (built from the Benthic source)
- `server.py` – Python standard-library server: serves the game, accounts, cloud saves, friends, chat, daily leaderboards, multiplayer lobbies and the AI answer judge (Groq)
- `deploy.sh` – installs or updates it on the Pi and checks GitHub for updates every 10 minutes

Install / update on the Pi:

```
curl -fsSL https://raw.githubusercontent.com/theguyofxavier/benthic/main/deploy.sh | bash
```

The Groq API key lives only in `.env` on the Pi and is never committed.
