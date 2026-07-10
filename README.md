# ChzzkBackup

Docker Compose 홈서버에서 실행하는 치지직/트윗캐스트 생방송 녹화 및 채팅 백업 대시보드입니다.

## 주요 기능

- 등록한 치지직 unique ID 또는 트윗캐스트 screen ID의 생방송 상태를 10초 주기로 감지합니다.
- 방송 중에는 임시 `.ts.part` 파일로 녹화합니다.
- 방송 중에는 채팅도 `./temp`의 `.part` 파일로 저장한 뒤 세션 종료 시 최종 채팅 폴더로 이동합니다.
- 방송 종료 후 CPU `libx264`, `veryfast`, CRF 28 설정으로 압축된 `.mp4` 파일을 생성합니다.
- 인코딩 중인 작업의 진행률, 처리 시간, 속도, 예상 남은 시간을 대시보드에 표시합니다.
- 치지직은 `chzzk-python`, 트윗캐스트는 TwitCasting API v2 댓글 조회로 채팅을 수집하고 JSONL, CSV 파일을 함께 저장합니다. JSONL에는 원본 이벤트를 보존하고 CSV는 기본 표시 컬럼만 저장합니다.
- Tailscale 또는 홈서버 내부망 사용을 전제로 인증 없는 웹 대시보드를 제공합니다.
- 브라우저별 로컬 설정으로 라이트/다크 모드를 전환할 수 있습니다.
- 채널, 토큰, 녹화 세션, 인코딩 큐, 로그 상태를 SQLite에 저장합니다.

## 경로

- 대시보드: `http://100.105.18.90:8733` (Tailscale 전용)
- 임시 녹화 파일: `./temp`
- 임시 채팅 파일: `./temp/*.chat.jsonl.part`, `./temp/*.chat.csv.part`
- 앱 DB 및 로그 상태: `./data`
- 최종 영상: `/home/bsubt/passport/chzzk_backup/<스트리머명>/`
- 트윗캐스트 최종 영상: `/home/bsubt/passport/chzzk_backup/트윗캐스트/<스트리머명>/`
- 채팅 파일: 각 최종 영상 폴더 아래 `채팅/`
- 저장 파일명 형식: `[YYMMDD HH-mm-ss] 스트리머명 - 제목.ext`
- 대시보드 로그 보관: 30일 이내, 동시에 최신 1000개까지만 유지

## 실행

```bash
docker compose build
docker compose up -d
```

Compose는 Tailscale 주소 `100.105.18.90:8733`에만 포트를 열고, 컨테이너를 UID/GID `1000:1000`으로 실행합니다. 헬스 엔드포인트는 `/health`입니다.

## 운영 방식

1. 대시보드에 접속합니다.
2. 치지직 계정의 `NID_SES`, `NID_AUT` 토큰을 저장합니다.
3. 트윗캐스트 채팅 수집이 필요하면 Read 권한이 있는 TwitCasting OAuth Access Token을 저장합니다.
4. 녹화할 치지직 unique ID 또는 트윗캐스트 screen ID를 등록합니다.
   - 트윗캐스트는 `screen_id`, `@screen_id`, `https://twitcasting.tv/screen_id` 형식을 받을 수 있습니다.
   - 트윗캐스트 비밀번호 방송은 지원하지 않습니다.
5. 등록된 채널이 생방송을 시작하면 자동으로 녹화와 채팅 저장을 시작합니다.
6. 방송 종료 후 인코딩 큐에서 `.mp4` 파일을 생성합니다.

녹화 세션의 제목은 대시보드에서 수정할 수 있으며, 최종 영상과 채팅 파일명에도 같은 제목이 반영됩니다.

기존 채팅 CSV를 새 표준 컬럼으로 통일하려면 다음 명령을 사용합니다.

```bash
uv run python -m app.chat_csv_migrate /home/bsubt/passport/chzzk_backup
```

## Git 설정

이 프로젝트의 원격 저장소는 다음 SSH 주소를 사용합니다.

```bash
git@github.com:bs6465/chzzkbackup.git
```

서버에 GitHub SSH 키가 없다면 다음 명령으로 키를 생성한 뒤, 출력된 공개키를 GitHub에 등록합니다.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_chzzkbackup -C "chzzkbackup-home-server"
cat ~/.ssh/id_ed25519_chzzkbackup.pub
```

## 주의사항

이 프로젝트는 개인 사용 목적의 비공식 도구이며 NAVER, CHZZK, TwitCasting과 관련이 없습니다. 스트림 녹화 방식 일부는 MIT 라이선스의 Chzzk-Rekoda 구현을 참고했습니다. 자세한 내용은 `THIRD_PARTY_NOTICES.md`를 확인하세요.
