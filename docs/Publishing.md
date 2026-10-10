# Publishing

## Repository description

**Slogan:** Mirror the present. Preserve the past.

**About:** Self-hosted Git backups that preserve captured history before force pushes, rewritten tags, and deletions—with a bilingual WebUI.

**中文说明：** 镜像当前状态，保全已有历史。自托管 Git 备份工具，在跟随强推、标签改写和引用删除前保留已采集的历史。

Suggested GitHub topics:

```text
git backup git-backup git-mirror self-hosted disaster-recovery
version-control gitea forgejo python fastapi docker webui
```

## Before the first publication

1. Choose the public repository owner/name. GitWarden uses [Apache License 2.0](../LICENSE); include the license in source and container distributions, and retain third-party license notices.
2. Verify the public branch includes only source, tests, example configuration, and public documentation. Review screenshots and commit author/committer metadata as well as file contents.
3. Run the checks in [Contributing](../CONTRIBUTING.md), then build and smoke-test the container. The workflow's first GitHub-hosted execution still needs to succeed after the repository is created.
4. Enable GitHub Actions and, if desired, private vulnerability reporting. Set the About description and topics above.
5. Publish only the reviewed public branch. Do not use `git push --all` or `--mirror` from a development repository containing private history.

## Versioned container releases

`VERSION` is the runtime release version, using `YYYYMMDD.N`. Check the current date before choosing a version and update [CHANGELOG.md](../CHANGELOG.md).

After the code has been published and CI passes, create a tag named `v` followed by the exact contents of `VERSION`. Publishing the tag starts the container release workflow. It checks the version and tests before publishing Linux amd64 and arm64 images to:

```text
ghcr.io/<owner>/<repository>:<VERSION>
ghcr.io/<owner>/<repository>:latest
```

The workflow obtains the repository identity from GitHub and uses its scoped `GITHUB_TOKEN`; no personal access token belongs in the repository. Make the GHCR package public if anonymous pulls are intended. The image must actually be published before users can pull it.

Verify the registry manifest includes both architectures and compare the running `/health` response with the release version and commit. Prefer a version tag for deployments. Keep the data volume when replacing a container; see [Deployment](wiki/Deployment.md).

The build workflow follows Docker's documented [multi-platform GitHub Actions flow](https://docs.docker.com/build/ci/github-actions/multi-platform/).

## Wiki

[docs/wiki/Home.md](wiki/Home.md) is the maintained user-guide entry point. It is readable directly in the repository without enabling GitHub Wiki.

To populate GitHub Wiki, enable it and create its first page in GitHub. Clone the Wiki repository locally, copy the Markdown files from `docs/wiki/` into that checkout. Before committing, adjust repository-relative image links to point to the public repository's raw image URLs and adjust README/CONTRIBUTING links to their public repository URLs. Review the rendered pages, then publish the Wiki separately. Publishing a source-code branch does not publish the separate Wiki repository.
