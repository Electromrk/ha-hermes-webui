# Hermes Agent (WebUI) for Home Assistant: the app image.
#
# All code is baked in under /opt. User data lives only in /config and /data, which Home
# Assistant mounts at runtime. Nothing is downloaded or installed at container start.
#
# amd64:   docker buildx build --platform linux/amd64 --load
#            -t local/hermes_webui:<version>-amd64 hermes_webui/
# aarch64: docker buildx build --platform linux/arm64 --build-arg BUILD_ARCH=aarch64 --load
#            -t local/hermes_webui:<version>-aarch64 hermes_webui/
#
# The Supervisor passes BUILD_ARCH and BUILD_VERSION itself.

# ---- Versions. The agent and the webui are a pair: always bump and test them together. ----

# Official agent image: release tag and multi-arch index digest (the digest decides).
# Source: https://hub.docker.com/r/nousresearch/hermes-agent/tags
# Digest: docker buildx imagetools inspect nousresearch/hermes-agent:<tag>
ARG HERMES_AGENT_TAG=v0.22.7
ARG HERMES_AGENT_DIGEST=sha256:e6d81ae52ebf6a43efa529701405509f20054ba818f179ddd748bed4f8af584c

# hermes-webui: a stable tag (v*, never exp-v*) and the commit it points to (the commit decides).
# Source: https://github.com/nesquena/hermes-webui/releases
ARG HERMES_WEBUI_TAG=v0.52.544
ARG HERMES_WEBUI_COMMIT=687d80333870c3797d6858738621fca13d1bf27a

# Plugin homeassistant 2.0.1, at its catalog commit.
# Source: https://github.com/NousResearch/hermes-official-plugins/tree/main/homeassistant
ARG HA_PLUGIN_COMMIT=4d492b1dc5326af3e38bb2cf3e4b9ad6d65f0072

# Plugin claude-subscription-directsdk 0.3.4, at the commit its marketplace page names.
# Source: https://github.com/NousResearch/hermes-official-plugins/tree/main/claude-subscription-directsdk
ARG CLAUDE_PLUGIN_COMMIT=e7a218f26db88426ab3e7376f0e000b6e356f5e7

# Claude Code CLI. The integrity hashes for every platform are in claude-code/package-lock.json.
# Source: https://www.npmjs.com/package/@anthropic-ai/claude-code
# A bump changes this line and, in claude-code/, runs
#   npm install --package-lock-only --save-exact @anthropic-ai/claude-code@<version>
# The build fails if this line and the lock file disagree.
ARG CLAUDE_CODE_VERSION=2.1.316

# App version for the labels, from hermes_webui/config.yaml.
ARG BUILD_VERSION=0.1.0

FROM nousresearch/hermes-agent:${HERMES_AGENT_TAG}@${HERMES_AGENT_DIGEST}

# The order below keeps rebuilds small: what changes most often comes last, and BUILD_VERSION
# is declared only at the very end, so a version bump alone reuses every layer.

# io.hass.arch uses Home Assistant's names, which a label cannot derive from TARGETARCH.
# This check fails fast, before anything is downloaded.
ARG TARGETARCH
ARG BUILD_ARCH=${TARGETARCH}
RUN case "${BUILD_ARCH}:$(uname -m)" in \
      amd64:x86_64|aarch64:aarch64) ;; \
      *) echo "BUILD_ARCH '${BUILD_ARCH}' does not fit machine $(uname -m). For arm64, pass --build-arg BUILD_ARCH=aarch64." >&2; exit 1 ;; \
    esac

# Debian packages. The base image leaves no apt lists, so update first.
# Package versions are not pinned: Debian drops superseded versions from its mirrors, so a pin
# would break later builds and block security fixes. The base digest fixes the Debian release.
# hadolint ignore=DL3008
RUN set -eu; \
    apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates jq nginx; \
    rm -rf /etc/nginx/sites-enabled /etc/nginx/conf.d \
      /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/debconf/*-old /var/lib/dpkg/*-old

# Claude Code CLI, as published: npm ci checks every package against the integrity hashes in the
# lock file, and nothing in the package is changed or removed. npm's caches stay out of the image.
# Its postinstall puts the native binary in place. npm 12 runs that only because claude-code/
# package.json allows it; the size check fails the build if the 500-byte stub is still there.
# The checks read files rather than run the binary, so this step works on every build host.
ARG CLAUDE_CODE_VERSION
WORKDIR /opt/claude-code
COPY claude-code/ ./
RUN set -eu; \
    HOME=/tmp/npm-build/home npm_config_cache=/tmp/npm-build/cache NODE_DISABLE_COMPILE_CACHE=1 \
      npm ci --no-audit --no-fund --omit=dev; \
    rm -rf /tmp/npm-build; \
    test "$(jq -r .version node_modules/@anthropic-ai/claude-code/package.json)" = "${CLAUDE_CODE_VERSION}"; \
    test "$(stat -L -c %s node_modules/.bin/claude)" -gt 100000000

# Plugins, verified by commit, without upstream tests and docs. The Home Assistant plugin is a
# bundled platform plugin. The Claude plugin stays outside every discovery path until the
# claude_subscription switch creates a symlink to it.
ARG HA_PLUGIN_COMMIT
ARG CLAUDE_PLUGIN_COMMIT
RUN set -eu; \
    src=/tmp/hermes-official-plugins; \
    checkout() { \
      git -C "${src}" fetch -q --depth 1 https://github.com/NousResearch/hermes-official-plugins.git "$1"; \
      git -C "${src}" checkout -q FETCH_HEAD; \
      test "$(git -C "${src}" rev-parse HEAD)" = "$1"; \
    }; \
    install_plugin() { \
      rm -rf "$2"; \
      cp -r "${src}/$1" "$2"; \
      rm -rf "$2/tests" "$2/evals" "$2/assets" "$2/.gitignore" "$2/AGENTS.md" "$2/CHANGELOG.md" \
        "$2/README.md" "$2/conftest.py" "$2/pytest.ini"; \
    }; \
    git init -q "${src}"; \
    checkout "${HA_PLUGIN_COMMIT}"; \
    install_plugin homeassistant /opt/hermes/plugins/platforms/homeassistant; \
    checkout "${CLAUDE_PLUGIN_COMMIT}"; \
    mkdir -p /opt/hermes-plugins; \
    install_plugin claude-subscription-directsdk /opt/hermes-plugins/claude-subscription-directsdk; \
    rm -rf "${src}"

# hermes-webui, verified by commit, without .git, tests, docs and the upstream launchers.
# It runs on the agent's venv and installs nothing: the app starts server.py directly.
# With no .git the webui reads its version from api/_version.py.
ARG HERMES_WEBUI_TAG
ARG HERMES_WEBUI_COMMIT
WORKDIR /opt/hermes-webui
RUN set -eu; \
    git init -q .; \
    git fetch -q --depth 1 https://github.com/nesquena/hermes-webui.git "${HERMES_WEBUI_COMMIT}"; \
    git checkout -q FETCH_HEAD; \
    test "$(git rev-parse HEAD)" = "${HERMES_WEBUI_COMMIT}"; \
    rm -rf .git .github .gitattributes .gitignore .dockerignore .env.example .env.docker.example \
      tests docs scripts nix \
      AGENTS.md ARCHITECTURE.md BUGS.md CHANGELOG.md CONTRIBUTING.md CONTRIBUTORS.md DESIGN.md \
      README.md ROADMAP.md SPRINTS.md TESTING.md THEMES.md \
      Dockerfile docker-compose*.yml docker_init.bash bootstrap.py ctl.sh start.sh start.ps1 \
      eslint.runtime-guard.config.mjs flake.lock flake.nix package.json pyproject.toml pytest.ini \
      requirements.txt requirements-dev.txt uv.lock; \
    printf '__version__ = "%s"\n' "${HERMES_WEBUI_TAG}" > api/_version.py

# Runtime environment. Everything the base image ties to /opt/data moves: HERMES_HOME, the write
# root, HOME and PATH (the base also adds /opt/data/.local/bin). The base still declares
# VOLUME /opt/data, which a derived image cannot undo; nothing here uses it.
# All caches stay under /tmp, so they never reach a backup. HOME is root's own, because tools running
# as root read their config from it, so the runtime user must not be able to write there. The runtime
# user's HOME, /tmp/hermes-home, is set for the webui in its run script.
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES=1 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    S6_VERBOSITY=1 \
    HERMES_HOME=/config/hermes \
    HERMES_WRITE_SAFE_ROOT=/config/hermes \
    HERMES_DISABLE_LAZY_INSTALLS=1 \
    HOME=/root \
    XDG_RUNTIME_DIR=/tmp/hermes-runtime \
    XDG_CACHE_HOME=/tmp/hermes-cache \
    UV_CACHE_DIR=/tmp/hermes-cache/uv \
    PIP_CACHE_DIR=/tmp/hermes-cache/pip \
    npm_config_cache=/tmp/hermes-cache/npm \
    CLAUDE_CONFIG_DIR=/data/claude \
    DISABLE_AUTOUPDATER=1 \
    DISABLE_UPDATES=1 \
    SHELL=/bin/bash \
    PATH=/opt/hermes-app/bin:/opt/hermes/bin:/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Our own process tree: no stock hook, no stock services. The stock hook seeds files and writes
# API_SERVER_KEY into .env. The root-drop shim stays, with its HOME moved off /opt/data; the grep
# makes the build fail if a new base image changes that line.
RUN set -eu; \
    rm -f /etc/cont-init.d/01-hermes-setup /etc/cont-init.d/015-supervise-perms \
      /etc/cont-init.d/02-reconcile-profiles \
      /etc/s6-overlay/s6-rc.d/user/contents.d/dashboard \
      /etc/s6-overlay/s6-rc.d/user/contents.d/main-hermes; \
    rm -rf /etc/s6-overlay/s6-rc.d/dashboard /etc/s6-overlay/s6-rc.d/main-hermes; \
    usermod -d /tmp/hermes-home hermes; \
    sed -i 's|^export HOME=/opt/data$|export HOME=/tmp/hermes-home|' /opt/hermes/bin/hermes; \
    grep -qx 'export HOME=/tmp/hermes-home' /opt/hermes/bin/hermes

# App files. rootfs also replaces the base's /etc/profile.d/hermes.sh, because /etc/profile resets
# PATH in login shells and the agent's terminal tool uses login shells.
COPY rootfs/ /tmp/app-rootfs/
COPY managed-config.yaml /etc/hermes/config.yaml

# COPY carries the build folder's file modes into the image, even onto /etc and /opt. A copy over
# Samba or a strict umask would lock the runtime user out, so every mode is set here, not inherited.
# The agent prepends /opt/hermes/libexec to PATH at import, so the hermes there must be our wrapper
# too. The last lines stop the build if anything we added brings repo or build leftovers along.
RUN set -eu; \
    find /tmp/app-rootfs -type d -exec chmod 00755 {} +; \
    find /tmp/app-rootfs -type f -exec chmod 0644 {} +; \
    cp -a --remove-destination /tmp/app-rootfs/. /; \
    rm -rf /tmp/app-rootfs; \
    chmod 0755 /opt/hermes-app/bin/* /opt/hermes-app/libexec-hermes /etc/s6-overlay/scripts/* \
      /etc/s6-overlay/s6-rc.d/*/run; \
    chmod 0644 /etc/hermes/config.yaml /opt/claude-code/package.json /opt/claude-code/package-lock.json; \
    mv /opt/hermes/libexec/hermes /opt/hermes/libexec/hermes.real; \
    mv /opt/hermes-app/libexec-hermes /opt/hermes/libexec/hermes; \
    mkdir -p /opt/hermes-app/empty-plugins; \
    test ! -e /config; \
    test ! -e /data; \
    leftovers="$(find /opt/hermes-webui /opt/hermes-plugins /opt/hermes-app /opt/claude-code \
      /opt/hermes/plugins/platforms/homeassistant \( -name .git -o -name __pycache__ -o -name tests \) -print)"; \
    test -z "${leftovers}" || { echo "Leftovers in the image: ${leftovers}" >&2; exit 1; }

WORKDIR /opt/hermes-webui
ENTRYPOINT ["/init"]
CMD []

# Labels last, so a new app version rebuilds nothing else.
# The licence label covers this app's own code; the baked Claude Code CLI has Anthropic's terms.
ARG BUILD_VERSION
ARG HERMES_AGENT_TAG
ARG HERMES_AGENT_DIGEST
LABEL io.hass.type="app" \
      io.hass.version="${BUILD_VERSION}" \
      io.hass.arch="${BUILD_ARCH}" \
      io.hass.name="Hermes Agent (WebUI)" \
      io.hass.description="Hermes Agent with the Hermes WebUI, in the Home Assistant sidebar" \
      org.opencontainers.image.title="Hermes Agent (WebUI)" \
      org.opencontainers.image.description="Hermes Agent with the Hermes WebUI, in the Home Assistant sidebar" \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.source="https://github.com/Electromrk/ha-hermes-webui" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="docker.io/nousresearch/hermes-agent:${HERMES_AGENT_TAG}" \
      org.opencontainers.image.base.digest="${HERMES_AGENT_DIGEST}"
