# Micra SmartThings

La Marzocco **Linea Micra**를 SmartThings에서 제어하는 커뮤니티 오픈소스 프로젝트입니다.
SmartThings Edge 드라이버와 NAS·미니 PC에서 실행하는 Python 중계 서버로 구성되며, Home Assistant는 필요하지 않습니다.

An unofficial SmartThings Edge driver and Python LAN bridge for the Linea Micra. An always-on bridge and a La Marzocco cloud account are required. UI labels are currently Korean.

**[Edge 드라이버 설치](https://bestow-regional.api.smartthings.com/invite/qyMZ8VR9b7lm)** · [릴리스](https://github.com/clience/micra-smartthings/releases) · [문제 신고](https://github.com/clience/micra-smartthings/issues)

## 동작 구조

```text
SmartThings 앱 ↔ SmartThings 허브의 Edge 드라이버
                         ↕ LAN HTTP / 인증 토큰
                   NAS·미니 PC의 Python 중계 서버
                         ↕ HTTPS / WebSocket
                   La Marzocco 클라우드 ↔ Micra
```

Edge 드라이버만 설치하면 동작하는 제품은 아닙니다. 중계 서버를 계속 켜 두어야 하며, 제조사 클라우드 연결이 필요합니다. Bluetooth는 아직 사용하지 않습니다.

클라우드 WebSocket 이벤트를 중계 서버가 받아 허브의 대기 중인 LAN 요청에 바로 전달합니다. 기본 30초 보조 조회는 누락된 상태를 확인하는 용도입니다. 명령 직후에는 3초 뒤 확인 조회도 수행하며, 명령 접수만으로 앱 상태를 바꾸지 않습니다.

## 지원 기능

| 항목 | SmartThings UI |
| --- | --- |
| 전원 / 대기 모드 | 켜기·끄기 스위치 |
| 스팀 보일러 | 사용 여부 |
| 스팀 강도 | 1–3단계 |
| 커피 보일러 목표 온도 | 80–100°C 슬라이더, 0.5°C 단위 |
| 프리브루잉 | 사용 여부 |
| 프리브루잉 물 공급·대기 시간 | 각각 1–9초, 0.5초 단위 |

실제 명령은 머신이 보고한 지원 범위·단위도 검사합니다. 프리브루잉 시간을 변경하려면 먼저 프리브루잉을 켜야 합니다. 전원 끄기는 클라우드의 대기 모드 전환이며 물리 전원 차단이 아닙니다.

통계, 최근 추출, 세척 상태, 물통 수위, 그라인더, Bluetooth 제어는 제공하지 않습니다. 통계는 공식 앱에서 확인하세요. Micra 한 대를 연결하는 구성입니다.

## 준비 사항

- LAN 통신이 가능한 SmartThings Edge 지원 허브와 SmartThings 앱
- 공식 La Marzocco 앱에 등록된 Linea Micra 및 이메일·비밀번호 로그인 계정
- 허브에서 접근할 수 있는 같은 LAN의 상시 가동 서버: NAS, Linux 미니 PC, Mac 등
- Python 3.13 이상 또는 Docker Compose
- 서버 LAN 주소 고정 또는 DHCP 예약

실기 확인 환경은 SmartThings Station, Synology NAS, Micra 게이트웨이 7.5.3입니다. 다른 허브·펌웨어 조합은 추가 검증이 필요합니다.

## 1. 중계 서버 설치

### Python으로 실행

```sh
git clone https://github.com/clience/micra-smartthings.git
cd micra-smartthings
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install .
micra-bridge configure
micra-bridge diagnose
micra-bridge show-token
```

`configure`에서 공식 앱의 이메일·비밀번호와 머신 시리얼을 입력합니다. 앱에는 머신이 보이는데 자동 검색이 0대로 나오면 시리얼을 지정해 다시 설정하세요. `diagnose`는 제어 명령 없이 로그인과 상태만 확인합니다. 출력에는 머신 식별 정보가 포함될 수 있습니다.

`show-token`에 나온 토큰은 뒤의 SmartThings 기기 설정에 사용합니다. 계정 정보는 `.local/config.json`, 인증 키는 `.local/installation-key.json`에 저장됩니다. 이 디렉터리를 비공개로 보관하세요.

서버의 **실제 LAN 주소**로 아래 예시 주소를 바꾸어 실행합니다.

```sh
micra-bridge serve --host 192.168.1.10 --port 8765 --enable-control
```

`--enable-control`을 생략하면 조회 전용입니다. 서버를 재부팅해도 실행되도록 운영체제의 서비스 관리자를 설정하거나 아래 Docker 방식을 사용하세요.

### Docker Compose로 실행

저장소를 내려받은 디렉터리에서 다음을 실행합니다. UID/GID는 데이터 폴더 소유자와 일치해야 합니다.

```sh
mkdir -p .local
chmod 700 .local
export MICRA_UID="$(id -u)"
export MICRA_GID="$(id -g)"
export MICRA_LAN_IP=192.168.1.10  # 서버의 실제 LAN IP로 변경
docker compose build
docker compose run --rm micra-bridge configure
docker compose run --rm micra-bridge diagnose
docker compose run --rm micra-bridge show-token
```

진단 후 `compose.yaml`의 `command`를 다음과 같이 바꾸어 제어를 활성화합니다.

```yaml
command: ["serve", "--host", "0.0.0.0", "--enable-control"]
```

```sh
docker compose up -d
docker compose logs --tail 30
```

`MICRA_LAN_IP`를 지정하지 않으면 기본적으로 로컬 호스트에만 포트가 열려 허브가 접근할 수 없습니다. 재배포 시에도 환경 변수를 유지하려면 로컬 `.env`에 저장하세요. `.env`는 Git에서 제외됩니다.

Linux NAS에서 host 네트워크가 필요한 경우 [compose.portainer.yaml](compose.portainer.yaml)을 참고하세요. 이미지를 먼저 빌드한 뒤 `MICRA_DATA_DIR`, `MICRA_UID`, `MICRA_GID`, `MICRA_LAN_IP`를 지정합니다. 이 예시도 기본값은 조회 전용이며, 제어하려면 `command`에 `--enable-control`을 추가합니다.

```sh
docker build -t micra-smartthings:0.3.2 .
```

## 2. 공개 Edge 드라이버 설치

1. **[Micra SmartThings 공개 배포 채널](https://bestow-regional.api.smartthings.com/invite/qyMZ8VR9b7lm)**에 접속하고 허브를 소유한 삼성 계정으로 로그인합니다.
2. 채널에서 사용할 허브를 등록하고 **Micra LAN Bridge** 드라이버를 설치합니다.
3. SmartThings 앱에서 **기기 추가 → 주변 검색**을 실행합니다.
4. 생성된 **Linea Micra** 기기의 설정에 아래 값을 입력합니다.

| 설정 | 입력값 |
| --- | --- |
| 중계 서버 LAN IP 주소 | NAS·미니 PC의 내부 IPv4 주소 |
| 중계 서버 포트 | 기본 `8765` |
| 중계 서버 접속 토큰 | `micra-bridge show-token`으로 확인한 값 |
| 보조 상태 확인 주기 | 기본 `30`초 |

IP에는 커피머신 주소가 아니라 **중계 서버 주소**를 입력합니다. Edge 드라이버는 사설 LAN IPv4만 허용하며 Tailscale·공인 IP는 받지 않습니다. 허브에서 중계 서버 포트로 접근할 수 있어야 합니다.

설정 저장 후 새로고침하고 전원·온도·프리브루잉 상태를 확인하세요. 공개 채널의 커스텀 capability는 함께 사용할 수 있으므로 일반 사용자가 별도로 등록할 필요가 없습니다.

## 문제 해결

- **오프라인:** 중계 서버 실행 여부, LAN IP·포트·토큰, 허브와 서버 사이 방화벽, 공식 앱의 머신 온라인 상태를 확인합니다.
- **조회는 되지만 제어되지 않음:** 서버의 `--enable-control` 옵션을 확인합니다.
- **머신 검색 결과가 0대:** `configure`에서 공식 앱에 표시된 시리얼을 지정합니다.
- **프리브루잉 시간 변경 실패:** 프리브루잉을 먼저 켜고 머신의 지원 범위를 확인합니다.
- **화면에 예전 카드가 남음:** 드라이버 업데이트가 적용됐는지 확인하고 기기 상세 화면을 다시 엽니다.

LAN 구간은 토큰 인증 HTTP입니다. 신뢰할 수 있는 내부 네트워크에서 사용하고 인터넷에 포트를 공개하지 마세요. 이슈에는 계정 정보, 토큰, 인증 키, 시리얼 또는 원본 진단 로그를 첨부하지 마세요.

## 개발 및 배포

```sh
python -m pip install '.[test]'
python -m pytest -q
python -m pip check
```

Python 중계 서버 테스트와 Lupa 기반 Lua 드라이버 테스트를 포함합니다. 실기 제어 없이 인증, 입력 범위, WebSocket 재연결·상태 처리, Edge 동작을 검증합니다.

- `micra_bridge/`: 클라우드 연결과 LAN API
- `edge/`: 허브에서 실행하는 Lua 드라이버와 프로필
- `capabilities/`: 커스텀 capability 정의, presentation, 번역
- `tests/`: 자동 검증

Edge 패키지 빌드:

```sh
mkdir -p dist
npx --yes @smartthings/cli@3.0.1 edge:drivers:package edge --build-only dist/micra-edge.zip
```

독립적인 포크를 배포하려면 `edge/config.yaml`의 `packageKey`를 변경하고, 본인의 SmartThings namespace로 capability를 등록한 다음 `edge/src/init.lua`와 프로필의 capability ID도 함께 변경하세요. 패키지를 업로드한 뒤 자신의 배포 채널에 할당합니다. 기존 공개 채널의 업데이트는 저장소 관리자만 배포합니다.

현재 공개 드라이버 버전: `2026-11-14T12:24:03.288558855` (bridge `0.3.2`).
공개 채널 ID: `eec2f5c8-e240-4fa1-a5f6-84bf3f478297`.

## 라이선스 및 감사

[MIT License](LICENSE). 클라우드 연결에는 [pylamarzocco](https://github.com/zweckj/pylamarzocco)를 사용합니다. 해당 라이브러리와 SmartThings Edge SDK의 기여자들에게 감사합니다.

La Marzocco 및 Samsung/SmartThings가 제작하거나 보증하는 공식 연동이 아닙니다. 제조사 API 변경으로 동작이 달라질 수 있습니다. 상표는 각 소유자에게 귀속됩니다.
