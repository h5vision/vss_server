#!/usr/bin/env bash
# vss_server EC2 준비 — Ubuntu 22.04/24.04, 기존 Ollama(11434)가 같은 머신에 떠 있는 팀 GPU 노드 기준.
#
#   bash scripts/setup_ec2.sh            # 1) 시스템 패키지 2) venv 3) PostgreSQL+pgvector 4) DB 초기화 5) 검증
#   SKIP_PG=1 bash scripts/setup_ec2.sh  # Chroma 만 쓸 때 (PostgreSQL 단계 건너뜀)
#
# 이 스크립트는 멱등입니다. 다시 돌려도 됩니다. sudo 가 필요합니다.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
RAG_PW="${VSS_RAG_PW:-vss_rag}"
SNAP_PW="${VSS_SNAP_PW:-vss_snapshot}"

echo "== 1. 시스템 패키지"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git curl jq

echo "== 2. Python venv (.venv — 학습용 .venv-train 과 섞지 않습니다)"
if [ ! -d .venv ]; then python3 -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt

if [ "${SKIP_PG:-0}" != "1" ]; then
  echo "== 3. PostgreSQL + pgvector"
  if ! command -v psql >/dev/null 2>&1; then
    sudo apt-get install -y -qq postgresql postgresql-contrib
  fi
  PGVER="$(ls /usr/lib/postgresql | sort -V | tail -1)"
  if ! sudo apt-get install -y -qq "postgresql-${PGVER}-pgvector" 2>/dev/null; then
    echo "   배포판 저장소에 pgvector 가 없어 PGDG 저장소를 추가합니다"
    sudo install -d /usr/share/postgresql-common/pgdg
    sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
      | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq "postgresql-${PGVER}-pgvector"
  fi
  sudo systemctl enable --now postgresql
  echo "== 4. DB 초기화 (vss / rag · snapshot 스키마 / 역할 2개)"
  # postgres 사용자는 /home/<계정> 을 통과할 수 없습니다(홈이 drwxr-x---). 상대경로도 절대경로도 못 읽습니다.
  # 그래서 파일은 이 셸(레포 소유자)이 읽고 psql 에는 stdin 으로 넘깁니다. cd /tmp 는 "could not change directory" 경고 제거용입니다.
  ( cd /tmp && sudo -u postgres psql -v rag_pw="'${RAG_PW}'" -v snap_pw="'${SNAP_PW}'" -f - ) < "$HERE/scripts/db_init.sql"
  export VSS_STORE=pgvector
  export VSS_PG_DSN="postgresql://vss_rag:${RAG_PW}@127.0.0.1:5432/vss"
  echo "== 5. pgvector 접속·벡터 왕복 검증"
  python - <<'PY'
import os, sys
sys.path.insert(0, ".")
from vss.store.pgvector import PgVectorStore
st = PgVectorStore()
b = st.begin_build("_setup_check", fingerprint={"embed_model": "bge-m3:latest", "embed_dim": 1024})
st.add(b, [{"path": "x.py", "type": "code", "text": "hello", "chunk_index": 0, "line_start": 1, "line_end": 1}],
       [[0.001] * 1024], project_id="_setup_check")
st.promote("_setup_check", b)
hits = st.query("_setup_check", [0.001] * 1024, 1)
assert hits and abs(hits[0]["score"] - 1.0) < 1e-4, hits
st.drop("_setup_check")
print("   pgvector OK — 벡터 1건 넣고 읽기 성공")
PY
  # 야간 백업 (매일 03:10, 7일 보관)
  sudo install -m 755 scripts/backup_pg.sh /usr/local/bin/vss-backup-pg
  ( sudo crontab -l 2>/dev/null | grep -v vss-backup-pg ; echo "10 3 * * * /usr/local/bin/vss-backup-pg" ) | sudo crontab -
  echo "   cron 등록: 매일 03:10 pg_dump → /var/backups/vss"
fi

echo "== 6. Ollama 확인"
OLLAMA="${VSS_OLLAMA_URL:-http://127.0.0.1:11434}"
if curl -fsS "${OLLAMA}/api/tags" >/dev/null; then
  curl -fsS "${OLLAMA}/api/tags" | jq -r '.models[].name' | sed 's/^/   /'
else
  echo "   !! Ollama 에 닿지 않습니다: ${OLLAMA}  (systemctl status ollama)"
fi

echo "== 7. 환경 파일 (.env) — systemd 와 셸이 같이 씁니다"
if [ ! -f .env ]; then
  cat > .env <<EOF
VSS_STORE=${VSS_STORE:-chroma}
VSS_PG_DSN=${VSS_PG_DSN:-postgresql://vss_rag:${RAG_PW}@127.0.0.1:5432/vss}
VSS_OLLAMA_URL=${OLLAMA}
VSS_CHAT_MODEL=qwen2.5-coder:7b
VSS_DATA_DIR=${HERE}/data
VSS_TOKEN=
EOF
  echo "   .env 생성 (토큰·모델은 여기서 바꿉니다)"
fi
# 인덱싱할 레포는 홈 아래에 둡니다 — sudo 없이 WinSCP·scp 로 바로 올릴 수 있습니다
mkdir -p data "$HOME/repos"

echo "== 8. systemd 서비스 (vss-server) — 포트 8200"
sed "s#__HERE__#${HERE}#g; s#__USER__#${USER}#g" scripts/vss-server.service | sudo tee /etc/systemd/system/vss-server.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable vss-server
echo "   시작:  sudo systemctl restart vss-server && journalctl -u vss-server -f"

echo "== 완료. 다음 명령으로 확인하세요:"
echo "   set -a; source .env; set +a; python -m vss.cli health"
