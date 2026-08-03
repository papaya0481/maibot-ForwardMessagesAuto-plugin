#!/usr/bin/env bash

set -euo pipefail

release_tag="${1:?Usage: prepare-public-release.sh <vX.Y.Z> [dev-ref] [main-ref]}"
dev_ref="${2:-origin/dev}"
main_ref="${3:-origin/main}"

if [[ ! "${release_tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "Error: the release tag must use the vX.Y.Z format; got ${release_tag}." >&2
    exit 1
fi

tag_ref="refs/tags/${release_tag}"
if ! git rev-parse --verify --quiet "${tag_ref}" >/dev/null; then
    echo "Error: release tag ${release_tag} was not found." >&2
    exit 1
fi

tag_type="$(git cat-file -t "${tag_ref}")"
if [[ "${tag_type}" != "tag" ]]; then
    echo "Error: ${release_tag} is not an annotated Git tag." >&2
    exit 1
fi

release_sha="$(git rev-list -n 1 "${tag_ref}")"
dev_sha="$(git rev-parse "${dev_ref}^{commit}")"
if [[ "${release_sha}" != "${dev_sha}" ]]; then
    echo "Error: ${release_tag} does not point to the current commit of the remote dev ref." >&2
    echo "tag=${release_sha} dev=${dev_sha}" >&2
    exit 1
fi

git switch --detach "${release_sha}"

manifest_version="$(jq -er '.version | select(type == "string" and length > 0)' _manifest.json)"
tag_version="${release_tag#v}"
if [[ "${manifest_version}" != "${tag_version}" ]]; then
    echo "Error: tag version ${tag_version} does not match the Manifest version ${manifest_version}." >&2
    exit 1
fi

# Development-only directories must not enter the public branch; remove them as a whole even if
# non-Markdown resources are added to them in the future.
git rm -r --ignore-unmatch -- .agents .claude .codex .github docs

# README.md is the only Markdown or similar documentation file allowed in the public tree.
while IFS= read -r -d '' path; do
    case "${path}" in
        README.md)
            ;;
        *.md|*.mdx|*.rst|*.adoc|*.MD|*.MDX|*.RST|*.ADOC)
            git rm -- "${path}"
            ;;
    esac
done < <(git ls-files -z)

# CHANGELOG.md is kept only on dev, so the public Manifest must not continue to reference it.
manifest_tmp="_manifest.public-release.tmp"
jq 'del(.changelog)' _manifest.json >"${manifest_tmp}"
mv "${manifest_tmp}" _manifest.json
git add _manifest.json

if [[ ! -f README.md ]]; then
    echo "Error: README.md is missing from the public release tree." >&2
    exit 1
fi

if grep -Eiq '\]\([^)]*(AGENTS\.md|CLAUDE\.md|CHANGELOG\.md|docs/|forward-message-design)' README.md; then
    echo "Error: README.md still links to documentation that exists only on dev." >&2
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
    echo "Error: the public release tree still contains additional documentation files:" >&2
    printf '  %s\n' "${unexpected_docs[@]}" >&2
    exit 1
fi

for forbidden_path in .agents .claude .codex .github docs; do
    if [[ -n "$(git ls-files -- "${forbidden_path}")" ]]; then
        echo "Error: the public release tree still contains ${forbidden_path}/." >&2
        exit 1
    fi
done

jq -e 'has("changelog") | not' _manifest.json >/dev/null
git diff --check

# Save the sanitized full tree first, then use the latest main as the parent commit so that the
# release PR shows only the tree changes from this release.
public_tree="$(git write-tree)"
git reset --hard "${release_sha}"
git switch --force-create public-release "${main_ref}"
git read-tree --reset -u "${public_tree}"

if git diff --cached --quiet; then
    echo "The public release tree is identical to main; no PR is needed."
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
    -m "chore: generate ${release_tag} public release" \
    -m "Generated from dev release commit ${release_sha}; retains only user-facing documentation."

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    {
        echo "has_changes=true"
        echo "release_tag=${release_tag}"
        echo "release_sha=${release_sha}"
    } >>"${GITHUB_OUTPUT}"
fi

echo "Generated public-release from ${release_tag}."
