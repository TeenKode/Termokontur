#!/usr/bin/env bash
# Debian 13 userland smoke: exercise native installer and updater using only
# disposable root filesystem and a mock systemctl (CI has no systemd PID 1).
set -Eeuo pipefail
trap 'status=$?; echo "Debian smoke failed at line $LINENO (status=$status)" >&2; if [[ -f /var/lib/termokontur/update-status.json ]]; then cat /var/lib/termokontur/update-status.json >&2; fi; exit "$status"' ERR
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git ca-certificates curl python3 python3-venv python3-pip tzdata passwd util-linux >/dev/null
mkdir -p /usr/local/bin
cat >/usr/local/bin/systemctl <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
start_web() {
  if [[ -f /run/termokontur-test.pid ]]; then
    start-stop-daemon --stop --oknodo --retry TERM/5/KILL/2 --pidfile /run/termokontur-test.pid >/dev/null 2>&1 || true
    rm -f /run/termokontur-test.pid
  fi
  set -a
  source /etc/termokontur/termokontur.env
  set +a
  export TERMOKONTUR_DB=/var/lib/termokontur/termokontur.sqlite3
  start-stop-daemon --start --background --make-pidfile \
    --pidfile /run/termokontur-test.pid --chuid termokontur \
    --chdir /opt/termokontur \
    --exec /opt/termokontur/.venv/bin/python -- \
    -m uvicorn termokontur.app:app --host 127.0.0.1 --port 8092
}
stop_web() {
  start-stop-daemon --stop --oknodo --retry TERM/5/KILL/2 --pidfile /run/termokontur-test.pid >/dev/null 2>&1 || true
  rm -f /run/termokontur-test.pid
}
verb="$1"
shift || true
case "$verb" in
  start)
    if [[ "${1:-}" == "termokontur.service" ]]; then start_web; fi ;;
  stop)
    if [[ "${1:-}" == "termokontur.service" ]]; then stop_web; fi ;;
  restart)
    if [[ "${1:-}" == "termokontur.service" ]]; then
      stop_web
      start_web
    fi ;;
  enable|daemon-reload|status|is-active) ;;
  *) echo "Unexpected mocked systemctl action: $verb $*" >&2; exit 1 ;;
esac
SH
chmod 0755 /usr/local/bin/systemctl
cat >/usr/local/bin/git <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == "clone" ]]; then
  destination="${!#}"
  mkdir -p "$destination"
  cp -r /src/. "$destination/"
  if [[ "${TEST_BREAK_UPGRADE:-0}" == "1" ]]; then
    printf '\nraise RuntimeError("intentional CI upgrade failure")\n' >> "$destination/termokontur/app.py"
  fi
  exit 0
fi
exec /usr/bin/git "$@"
SH
chmod 0755 /usr/local/bin/git
export PATH="/usr/local/bin:$PATH"
# Trust only the read-only CI checkout, never every repository.
git config --global --add safe.directory /src

cd /src
bash scripts/install-debian.sh
test -e /etc/systemd/system/termokontur-update.service
grep -q '^AmbientCapabilities=CAP_NET_BIND_SERVICE
test -x /usr/local/sbin/termokontur-apply-update
systemctl start termokontur.service
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8092/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8092/health
set -a
source /etc/termokontur/termokontur.env
set +a
curl --silent --show-error --fail -c /tmp/termokontur-ci-cookies \
  -H 'Content-Type: application/json' \
  -d "{\"password\":\"$TERMOKONTUR_ADMIN_PASSWORD\"}" \
  http://127.0.0.1:8092/api/login >/dev/null
curl --silent --show-error --fail -b /tmp/termokontur-ci-cookies \
  -H 'Content-Type: application/json' \
  -d '{"building":"CI Test","name":"101","min_temp":18,"max_temp":24}' \
  http://127.0.0.1:8092/api/rooms >/dev/null

echo "Begin native updater smoke assertions"
# Force a change to exercise the actual stage/swap path of the updater.
printf '%040d\n' 0 > /var/lib/termokontur/installed-revision
runuser -u termokontur -- touch /var/lib/termokontur/update-request
/usr/local/sbin/termokontur-apply-update
echo "Updater returned; validating post-update state"
cat /var/lib/termokontur/update-status.json
grep -q '"state": "success"' /var/lib/termokontur/update-status.json
echo "Checking previous application backup"
test -d /opt/termokontur-previous
test -d /var/lib/termokontur/backups
test -n "$(find /var/lib/termokontur/backups -name 'pre-update-*' -print -quit)"
echo "Comparing installed privileged system components"
cmp /src/deploy/systemd/termokontur-update.service /etc/systemd/system/termokontur-update.service
cmp /src/scripts/termokontur-apply-update /usr/local/sbin/termokontur-apply-update
curl --silent --show-error --fail -b /tmp/termokontur-ci-cookies \
  http://127.0.0.1:8092/api/rooms | grep -q 'CI Test'

# Inject a startup failure into the staged application; rollback must bring
# back the previous working files and SQLite data.
printf '%040d\n' 0 > /var/lib/termokontur/installed-revision
runuser -u termokontur -- touch /var/lib/termokontur/update-request
if TEST_BREAK_UPGRADE=1 /usr/local/sbin/termokontur-apply-update; then
  echo "Broken upgrade unexpectedly succeeded" >&2
  exit 1
fi
grep -q '"state": "failed"' /var/lib/termokontur/update-status.json
echo "Checking rollback health after process startup"
recovered=0
for i in $(seq 1 25); do
  if curl --silent --show-error --fail http://127.0.0.1:8092/health; then
    recovered=1
    break
  fi
  sleep 1
done
test "$recovered" = 1
curl --silent --show-error --fail -b /tmp/termokontur-ci-cookies \
  http://127.0.0.1:8092/api/rooms | grep -q 'CI Test'
echo "Debian 13 native install, update and rollback smoke succeeded."
 /etc/systemd/system/termokontur.service
test -x /usr/local/sbin/termokontur-apply-update
systemctl start termokontur.service
for i in $(seq 1 30); do
  if curl -fsS --max-time 2 http://127.0.0.1:8092/health >/dev/null; then break; fi
  sleep 1
done
curl -fsS http://127.0.0.1:8092/health
set -a
source /etc/termokontur/termokontur.env
set +a
curl --silent --show-error --fail -c /tmp/termokontur-ci-cookies \
  -H 'Content-Type: application/json' \
  -d "{\"password\":\"$TERMOKONTUR_ADMIN_PASSWORD\"}" \
  http://127.0.0.1:8092/api/login >/dev/null
curl --silent --show-error --fail -b /tmp/termokontur-ci-cookies \
  -H 'Content-Type: application/json' \
  -d '{"building":"CI Test","name":"101","min_temp":18,"max_temp":24}' \
  http://127.0.0.1:8092/api/rooms >/dev/null

echo "Begin native updater smoke assertions"
# Force a change to exercise the actual stage/swap path of the updater.
printf '%040d\n' 0 > /var/lib/termokontur/installed-revision
runuser -u termokontur -- touch /var/lib/termokontur/update-request
/usr/local/sbin/termokontur-apply-update
echo "Updater returned; validating post-update state"
cat /var/lib/termokontur/update-status.json
grep -q '"state": "success"' /var/lib/termokontur/update-status.json
echo "Checking previous application backup"
test -d /opt/termokontur-previous
test -d /var/lib/termokontur/backups
test -n "$(find /var/lib/termokontur/backups -name 'pre-update-*' -print -quit)"
echo "Comparing installed privileged system components"
cmp /src/deploy/systemd/termokontur-update.service /etc/systemd/system/termokontur-update.service
cmp /src/scripts/termokontur-apply-update /usr/local/sbin/termokontur-apply-update
curl --silent --show-error --fail -b /tmp/termokontur-ci-cookies \
  http://127.0.0.1:8092/api/rooms | grep -q 'CI Test'

# Inject a startup failure into the staged application; rollback must bring
# back the previous working files and SQLite data.
printf '%040d\n' 0 > /var/lib/termokontur/installed-revision
runuser -u termokontur -- touch /var/lib/termokontur/update-request
if TEST_BREAK_UPGRADE=1 /usr/local/sbin/termokontur-apply-update; then
  echo "Broken upgrade unexpectedly succeeded" >&2
  exit 1
fi
grep -q '"state": "failed"' /var/lib/termokontur/update-status.json
echo "Checking rollback health after process startup"
recovered=0
for i in $(seq 1 25); do
  if curl --silent --show-error --fail http://127.0.0.1:8092/health; then
    recovered=1
    break
  fi
  sleep 1
done
test "$recovered" = 1
curl --silent --show-error --fail -b /tmp/termokontur-ci-cookies \
  http://127.0.0.1:8092/api/rooms | grep -q 'CI Test'
echo "Debian 13 native install, update and rollback smoke succeeded."
