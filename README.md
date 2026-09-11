# ai-token-rpg

Claude Code와 Codex의 로컬 토큰 사용량을 RPG 캐릭터 성장으로 바꿔 GitHub 프로필에 고정한 Gist에 표시합니다.

```text
🧙 Lv.54/200 · Context Mage · created Feb 3, 2026
XP 🟪🟪🟪🟪🟪⬛⬛⬛⬛⬛ 47.9% · 123.9M to Lv.55
📊 2.9B tokens · ~$3,112.96
🔥 today 58.9M · peak 282.4M (Jun 20)
🤖 Claude 2.9B (99.9%) · Codex 1.5M (0.1%)
🕐 updated 2026-07-20 23:00 KST
```

GitHub 프로필에 고정한 Gist에는 첫 5줄이 보이고, 마지막 갱신 시각은 Gist를 열면 확인할 수 있습니다.

## 주요 기능

- `~/.claude/projects/**/*.jsonl`에서 Claude Code 사용량 집계
- `~/.codex/sessions`와 `~/.codex/archived_sessions`에서 Codex 사용량 집계
- 총 토큰에 따른 Lv.1~200 비선형 성장
- Claude/Codex 비중과 레벨에 따른 직업 진화
- 레벨 등급에 따라 바뀌는 10칸 XP 게이지
- 날짜별 집계 JSON과 카드의 Gist 동시 백업
- Claude Code와 Codex `Stop` 훅 자동 갱신
- 30분 기본 push 간격과 파일 잠금으로 중복 호출 방지

메시지, 프롬프트, 응답, 도구 출력, 프로젝트 경로는 저장하지 않습니다. timestamp, 모델 식별자, 토큰 카운터만 집계에 사용합니다.

## 설치

준비물은 Python 3.9 이상과 GitHub CLI(`gh`)입니다.

```sh
git clone https://github.com/jeongph/ai-token-rpg.git ~/Dev/lighthouse/repositories/ai-token-rpg
cd ~/Dev/lighthouse/repositories/ai-token-rpg
gh auth login
gh auth refresh -h github.com -s gist
```

공개 Gist를 생성합니다.

```sh
printf 'loading...\n' | gh gist create --public --filename ai-token-rpg -d 'AI Token RPG card'
```

출력 URL의 마지막 ID를 환경 변수에 등록합니다.

```sh
export AI_TOKEN_RPG_GIST_ID=<GIST_ID>
```

처음 실행하고 Gist에 업로드합니다.

```sh
python3 src/ai_token_rpg.py --push --force
```

기존 `ai-usage-card/data.json`을 이어받으려면 최초 실행에만 다음 옵션을 사용합니다. 기존 형식은 Claude 데이터로 자동 변환됩니다.

```sh
python3 src/ai_token_rpg.py \
  --import-legacy ../ai-usage-card/data.json \
  --push --force
```

GitHub 프로필의 **Customize your pins**에서 생성한 Gist를 선택합니다.

## 자동 갱신

두 훅이 같은 스크립트를 동시에 호출하면 파일 잠금을 사용해 한 번에 하나씩 처리합니다. 로컬 데이터는 매번 갱신하고, Gist push는 기본 30분에 한 번만 실행합니다.

### Claude Code

기존 설정과 병합해 `~/.claude/settings.json`에 추가합니다.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "AI_TOKEN_RPG_GIST_ID=<GIST_ID> python3 $HOME/Dev/lighthouse/repositories/ai-token-rpg/src/ai_token_rpg.py --push --quiet"
          }
        ]
      }
    ]
  }
}
```

### Codex

Codex는 공식적으로 turn-scoped `Stop` command hook을 지원합니다. 기존 설정과 병합해 `~/.codex/hooks.json`에 추가합니다.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "AI_TOKEN_RPG_GIST_ID=<GIST_ID> python3 $HOME/Dev/lighthouse/repositories/ai-token-rpg/src/ai_token_rpg.py --push --quiet",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

Codex를 다시 시작한 다음 `/hooks`에서 새 command hook의 내용을 검토하고 신뢰 처리합니다. 자세한 동작은 [Codex Hooks 문서](https://learn.chatgpt.com/docs/hooks)를 참고하세요.

Codex hook이 전달하는 transcript 경로는 편의 기능이며 transcript 포맷 자체는 안정 인터페이스가 아닙니다. 이 프로젝트의 Codex 파서는 알 수 없는 레코드와 불완전한 마지막 줄을 무시하고, 테스트 fixture로 현재 포맷을 검증합니다.

## 레벨 시스템

최대 레벨은 200, 최종 필요 경험치는 1T tokens입니다. 초반에는 빠르게 성장하고 후반으로 갈수록 다음 레벨에 필요한 토큰 수가 늘어납니다.

```python
level = floor(200 * (total_tokens / 1_000_000_000_000) ** (1 / 4.5))
level = min(200, max(1, level))
```

| 레벨 | 등급 | 게이지 |
|---:|---|:---:|
| 1–19 | Common | 🟩 |
| 20–39 | Rare | 🟦 |
| 40–69 | Epic | 🟪 |
| 70–99 | Legendary | 🟨 |
| 100–149 | Mythic | 🟧 |
| 150–199 | Transcendent | 🟥 |
| 200 | MAX | 💠 |

### 직업 진화

한 공급자의 누적 비중이 65% 이상이면 해당 직업 경로를 선택하고, 아니면 Hybrid 경로를 선택합니다.

| 레벨 | Claude 중심 | Codex 중심 | Hybrid |
|---:|---|---|---|
| 1–19 | Prompt Scribe | Code Scout | Dual Novice |
| 20–39 | Context Weaver | Code Smith | Hybrid Adept |
| 40–69 | Context Mage | Code Knight | Dual Caster |
| 70–99 | Reasoning Sage | Repo Architect | AI Orchestrator |
| 100–149 | Oracle | Forge Master | Singularity Engineer |
| 150–199 | Cosmic Oracle | Code Sovereign | Agent Overlord |
| 200 | Token Ascendant | Token Ascendant | Token Ascendant |

## 토큰 계산

- Claude: `input + output + cache_creation + cache_read`
- Codex: `input + output`
- Codex의 `cached_input`은 input의 부분집합이고 `reasoning_output`은 output의 상세값이므로 총량에 다시 더하지 않습니다.
- Codex `token_count`는 세션 누적값이므로 인접 이벤트의 차이만 해당 날짜에 반영합니다.
- 동일 Codex 세션이 active와 archived 위치에 함께 있어도 session ID와 스냅샷으로 중복 제거합니다.

비용은 가격표에 등록된 Claude 모델의 API 정가 환산값입니다. 구독 결제액이 아니며, 공개 API 가격과 확실히 연결되지 않는 Codex 내부 모델의 비용은 포함하지 않습니다.

## 데이터 보존

로컬 파일은 Git에 커밋하지 않습니다.

| 파일 | 용도 |
|---|---|
| `src/data.json` | 날짜·공급자별 누적 집계 |
| `src/card.txt` | 렌더링된 카드 |
| `src/.last_push` | Gist push 간격 기록 |
| `src/.token-rpg.lock` | 동시 훅 실행 잠금 |

`--push` 시 Gist에 다음 두 파일을 함께 저장합니다.

- `ai-token-rpg`: 프로필 카드
- `ai-token-rpg-data.json`: 메시지 내용이 없는 누적 집계 백업

새 컴퓨터에서 같은 Gist ID로 `--push --force`를 실행하면 원격 집계를 먼저 가져와 로컬 데이터와 병합합니다. 로그가 삭제되거나 컴퓨터를 포맷해도 Gist 백업이 남아 있으면 누적 기록을 이어갈 수 있습니다.

공개 Gist에는 날짜별 토큰 수와 추정 비용이 노출됩니다. 이 정보까지 공개하고 싶지 않다면 집계 백업용 private Gist를 분리하는 기능이 추가로 필요합니다.

## 옵션

| 환경 변수 | 기본값 | 설명 |
|---|---|---|
| `AI_TOKEN_RPG_GIST_ID` | 없음 | 카드와 백업을 저장할 Gist ID 또는 URL |
| `AI_TOKEN_RPG_TZ` | `Asia/Seoul` | 날짜 집계 타임존 |
| `AI_TOKEN_RPG_PUSH_INTERVAL_MINUTES` | `30` | 최소 Gist push 간격 |
| `AI_TOKEN_RPG_CARD_FILENAME` | `ai-token-rpg` | Gist 카드 파일명 |
| `AI_TOKEN_RPG_DATA_FILENAME` | `ai-token-rpg-data.json` | Gist 백업 파일명 |
| `CLAUDE_LOG_ROOT` | `~/.claude/projects` | Claude Code 로그 루트 |
| `CODEX_HOME` | `~/.codex` | Codex 로컬 상태 루트 |

수동 출력만 확인하려면:

```sh
python3 src/ai_token_rpg.py
```

push 제한을 무시하려면:

```sh
python3 src/ai_token_rpg.py --push --force
```

## 개발

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest
```

