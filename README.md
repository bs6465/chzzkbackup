# ChzzkBackup

Docker Compose 홈서버에서 실행하는 치지직 생방송 녹화 및 채팅 백업 대시보드입니다.

## 주요 기능

- 등록한 스트리머 unique ID의 치지직 생방송 상태를 주기적으로 감지합니다.
- 방송 중에는 임시 `.ts.part` 파일로 녹화합니다.
- 방송 종료 후 CPU `libx264`, `veryfast`, CRF 28 설정으로 압축된 `.mp4` 파일을 생성합니다.
- `chzzk-python`을 사용해 실시간 채팅을 수집하고 JSONL, CSV 파일을 함께 저장합니다.
- Tailscale 또는 홈서버 내부망 사용을 전제로 인증 없는 웹 대시보드를 제공합니다.
- 채널, 토큰, 녹화 세션, 인코딩 큐, 로그 상태를 SQLite에 저장합니다.

## 경로

- 대시보드: `http://<서버 IP>:8733`
- 임시 녹화 파일: `./temp`
- 앱 DB 및 로그 상태: `./data`
- 최종 영상: `/home/bsubt/passport/chzzk_backup/<스트리머명>/`
- 채팅 파일: `/home/bsubt/passport/chzzk_backup/<스트리머명>/채팅/`

## 실행

```bash
docker compose build
docker compose up -d
```

Compose는 `0.0.0.0:8733:8733`으로 포트를 열고, 컨테이너를 UID/GID `1000:1000`으로 실행합니다.

## 운영 방식

1. 대시보드에 접속합니다.
2. 치지직 계정의 `NID_SES`, `NID_AUT` 토큰을 저장합니다.
3. 녹화할 스트리머 unique ID를 등록합니다.
4. 등록된 채널이 생방송을 시작하면 자동으로 녹화와 채팅 저장을 시작합니다.
5. 방송 종료 후 인코딩 큐에서 `.mp4` 파일을 생성합니다.

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

이 프로젝트는 개인 사용 목적의 비공식 도구이며 NAVER 또는 CHZZK와 관련이 없습니다. 스트림 녹화 방식 일부는 MIT 라이선스의 Chzzk-Rekoda 구현을 참고했습니다. 자세한 내용은 `THIRD_PARTY_NOTICES.md`를 확인하세요.
