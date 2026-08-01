#!/usr/bin/env bash

set -euo pipefail

release_tag="${1:?用法: prepare-public-release.sh <vX.Y.Z> [dev-ref] [main-ref]}"
dev_ref="${2:-origin/dev}"
main_ref="${3:-origin/main}"

if [[ ! "${release_tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "错误：发布 tag 必须使用 vX.Y.Z 格式，实际为 ${release_tag}。" >&2
    exit 1
fi

tag_ref="refs/tags/${release_tag}"
if ! git rev-parse --verify --quiet "${tag_ref}" >/dev/null; then
    echo "错误：找不到发布 tag ${release_tag}。" >&2
    exit 1
fi

tag_type="$(git cat-file -t "${tag_ref}")"
if [[ "${tag_type}" != "tag" ]]; then
    echo "错误：${release_tag} 不是带注释的 Git tag。" >&2
    exit 1
fi

release_sha="$(git rev-list -n 1 "${tag_ref}")"
dev_sha="$(git rev-parse "${dev_ref}^{commit}")"
if [[ "${release_sha}" != "${dev_sha}" ]]; then
    echo "错误：${release_tag} 未指向远端 dev 当前 commit。" >&2
    echo "tag=${release_sha} dev=${dev_sha}" >&2
    exit 1
fi

git switch --detach "${release_sha}"

manifest_version="$(jq -er '.version | select(type == "string" and length > 0)' _manifest.json)"
tag_version="${release_tag#v}"
if [[ "${manifest_version}" != "${tag_version}" ]]; then
    echo "错误：tag 版本 ${tag_version} 与 Manifest 版本 ${manifest_version} 不一致。" >&2
    exit 1
fi

# 开发专用目录不会进入公开分支；即使未来其中加入非 Markdown 资源也会整体删除。
git rm -r --ignore-unmatch -- .agents .claude .codex .github docs

# README.md 是公开树中唯一允许保留的 Markdown 或类似文档文件。
while IFS= read -r -d '' path; do
    case "${path}" in
        README.md)
            ;;
        *.md|*.mdx|*.rst|*.adoc|*.MD|*.MDX|*.RST|*.ADOC)
            git rm -- "${path}"
            ;;
    esac
done < <(git ls-files -z)

# CHANGELOG.md 只保留在 dev，因此公开 Manifest 不能继续指向该文件。
manifest_tmp="_manifest.public-release.tmp"
jq 'del(.changelog)' _manifest.json >"${manifest_tmp}"
mv "${manifest_tmp}" _manifest.json
git add _manifest.json

if [[ ! -f README.md ]]; then
    echo "错误：公开发布树缺少 README.md。" >&2
    exit 1
fi

if grep -Eiq '\]\([^)]*(AGENTS\.md|CLAUDE\.md|CHANGELOG\.md|docs/|forward-message-design)' README.md; then
    echo "错误：README.md 仍链接到仅存在于 dev 的文档。" >&2
    exit 1
fi

unexpected_docs=()
while IFS= read -r -d '' path; do
    case "${path}" in
        README.md)
            ;;
        *.md|*.mdx|*.rst|*.adoc|*.MD|*.MDX|*.RST|*.ADOC)
            unexpected_docs+=("${path}")
            ;;
    esac
done < <(git ls-files -z)

if (( ${#unexpected_docs[@]} > 0 )); then
    echo "错误：公开发布树仍包含额外文档：" >&2
    printf '  %s\n' "${unexpected_docs[@]}" >&2
    exit 1
fi

for forbidden_path in .agents .claude .codex .github docs; do
    if [[ -n "$(git ls-files -- "${forbidden_path}")" ]]; then
        echo "错误：公开发布树仍包含 ${forbidden_path}/。" >&2
        exit 1
    fi
done

jq -e 'has("changelog") | not' _manifest.json >/dev/null
git diff --check

# 先保存净化后的完整树，再以最新 main 为父提交，确保发布 PR 只展示本次树差异。
public_tree="$(git write-tree)"
git reset --hard "${release_sha}"
git switch --force-create public-release "${main_ref}"
git read-tree --reset -u "${public_tree}"

if git diff --cached --quiet; then
    echo "公开发布树与 main 完全一致，无需创建 PR。"
    if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
        {
            echo "has_changes=false"
            echo "release_tag=${release_tag}"
            echo "release_sha=${release_sha}"
        } >>"${GITHUB_OUTPUT}"
    fi
    exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git commit \
    -m "chore: 生成 ${release_tag} 公开版本" \
    -m "从 dev 发布 commit ${release_sha} 生成，仅保留面向用户的文档。"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
        echo "has_changes=true"
        echo "release_tag=${release_tag}"
        echo "release_sha=${release_sha}"
    } >>"${GITHUB_OUTPUT}"
fi

echo "已从 ${release_tag} 生成 public-release。"
