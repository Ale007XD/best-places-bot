#!/usr/bin/env bash
# Создаёт pull request с изменениями из указанных путей (если они есть).
#   open_pr.sh <префикс-ветки> <заголовок> <файл-описания> <путь>...
# Нужны GH_TOKEN и права contents/pull-requests: write. PR от GITHUB_TOKEN не запускает CI сам —
# откройте его и нажмите «Re-run»/закройте-переоткройте, либо используйте PAT.
set -euo pipefail
prefix="$1"; title="$2"; body="$3"; shift 3
if git diff --quiet -- "$@"; then
  echo "изменений нет — PR не нужен"
  exit 0
fi
branch="${prefix}-${GITHUB_RUN_ID:-local}"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git checkout -b "$branch"
git add -- "$@"
git commit -m "$title"
git push origin "$branch"
gh pr create --base main --head "$branch" --title "$title" --body-file "$body"
