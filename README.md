# ChzzkBackup

Local CHZZK live recorder and chat backup dashboard for a Docker Compose home server.

## Features

- Detects active CHZZK live streams for registered streamer unique IDs.
- Records live streams to temporary `.ts.part` files.
- Converts completed recordings to compressed `.mp4` files with CPU `libx264`, `veryfast`, CRF 28.
- Captures real-time chat with `chzzk-python` and writes both JSONL and CSV files.
- Provides a no-auth dashboard for Tailscale/home-server use.
- Stores app state in SQLite.

## Paths

- Dashboard: `http://<server-ip>:8733`
- Temp recordings: `./temp`
- App DB/log state: `./data`
- Final videos: `/home/bsubt/passport/chzzk_backup/<streamer_name>/`
- Chat files: `/home/bsubt/passport/chzzk_backup/<streamer_name>/채팅/`

## Run

```bash
docker compose build
docker compose up -d
```

The compose file binds `0.0.0.0:8733:8733` and runs the container as UID/GID `1000:1000`.

## Git Setup

This project is intended to push directly to:

```bash
git@github.com:bs6465/chzzkbackup.git
```

If this server has no GitHub SSH key yet:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_chzzkbackup -C "chzzkbackup-home-server"
cat ~/.ssh/id_ed25519_chzzkbackup.pub
```

Add the printed public key to GitHub, then configure SSH to use it.

## Notes

This is an unofficial personal tool and is not affiliated with NAVER or CHZZK. The reused stream recording approach is derived from Chzzk-Rekoda under the MIT license; see `THIRD_PARTY_NOTICES.md`.
