#!/bin/sh
# tepra-web の .deb を作る。
#
#   packaging/build-deb.sh [出力ディレクトリ]
#
# ドライバ (driver/, Rust) をビルドして server.py とまとめ、systemd の
# ユニットと udev ルールを同梱する。インストールするとサービスが有効化
# されて起動する。
#
# 必要なもの: cargo, dpkg-deb, fakeroot
# バージョン: git のタグ (v1.2.3) があればそれ。無ければ日付と短い SHA。
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)
outdir=${1:-$root/dist}
arch=$(dpkg --print-architecture)

version=$(git -C "$root" describe --tags --match 'v*' --exact-match 2>/dev/null | sed 's/^v//' || true)
if [ -z "$version" ]; then
    date=$(git -C "$root" log -1 --format=%cd --date=format:%Y%m%d 2>/dev/null || date +%Y%m%d)
    sha=$(git -C "$root" rev-parse --short HEAD 2>/dev/null || echo unknown)
    version="0.0.0~git$date.$sha"
fi

echo "building tepra-web $version ($arch)"

# --- ドライバ ---------------------------------------------------------
if [ ! -f "$root/driver/Cargo.toml" ]; then
    echo "driver/ is empty: run git submodule update --init" >&2
    exit 1
fi
cargo build --release --manifest-path "$root/driver/Cargo.toml"

# --- ツリーを組む -----------------------------------------------------
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
# mktemp は 0700 で作るが、パッケージのルートは 0755 でなければならない
chmod 0755 "$stage"

# usrmerge 済みの環境では /lib はシンボリックリンクなので、実体のある
# /usr/lib 側に置く
install -d "$stage/usr/lib/tepra-web" \
           "$stage/etc/tepra-web" \
           "$stage/usr/lib/systemd/system" \
           "$stage/usr/lib/udev/rules.d" \
           "$stage/usr/share/doc/tepra-web" \
           "$stage/DEBIAN"

install -m 0755 "$root/driver/target/release/tpsr3700" "$stage/usr/lib/tepra-web/tpsr3700"
install -m 0644 "$root/server.py" "$stage/usr/lib/tepra-web/server.py"
install -m 0644 "$here/tepra-web.conf" "$stage/etc/tepra-web/tepra-web.conf"
install -m 0644 "$here/tepra-web.service" "$stage/usr/lib/systemd/system/tepra-web.service"
install -m 0644 "$here/99-tepra.rules" "$stage/usr/lib/udev/rules.d/99-tepra.rules"
install -m 0644 "$root/README.md" "$stage/usr/share/doc/tepra-web/README.md"
install -m 0644 "$root/driver/docs/protocol.md" "$stage/usr/share/doc/tepra-web/protocol.md"

# フォントはドライバに BIZ UDPゴシックが埋め込まれている。ここに置くのは
# 追加フォントだけなので、空のディレクトリを作るにとどめる (postinst)。

cat > "$stage/DEBIAN/control" <<EOF
Package: tepra-web
Version: $version
Section: utils
Priority: optional
Architecture: $arch
Depends: python3 (>= 3.8), adduser, systemd, udev
Maintainer: kazubu <kazubu@users.noreply.github.com>
Homepage: https://github.com/kazubu/tepra-web
Description: Web front end for KING JIM TEPRA label printers
 Prints text labels on a TEPRA PRO (SR3700P / SR3900P / SR750) over USB
 from a browser, with preview, templates, merge printing from CSV, and
 an HTTP API for scripts. Includes the tpsr3700 driver.
 .
 Installs a systemd service listening on port 8080 and a udev rule that
 opens the printer to the lp group. There is no authentication: use it
 on a trusted network or put a reverse proxy in front.
EOF

cat > "$stage/DEBIAN/conffiles" <<'EOF'
/etc/tepra-web/tepra-web.conf
EOF

cat > "$stage/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

case "$1" in
configure)
    if ! getent passwd tepra-web >/dev/null; then
        adduser --system --group --no-create-home \
                --home /var/lib/tepra-web \
                --gecos "TEPRA Web printing server" tepra-web
    fi
    install -d -o tepra-web -g tepra-web -m 0755 /var/lib/tepra-web /var/lib/tepra-web/fonts

    if [ -x /bin/udevadm ] || [ -x /sbin/udevadm ]; then
        udevadm control --reload-rules >/dev/null 2>&1 || true
        udevadm trigger --subsystem-match=usb >/dev/null 2>&1 || true
    fi

    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl enable --now tepra-web.service >/dev/null 2>&1 || true
    fi
    ;;
esac

exit 0
EOF

cat > "$stage/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e

case "$1" in
remove|deconfigure)
    if [ -d /run/systemd/system ]; then
        systemctl stop tepra-web.service >/dev/null 2>&1 || true
        systemctl disable tepra-web.service >/dev/null 2>&1 || true
    fi
    ;;
esac

exit 0
EOF

cat > "$stage/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e

case "$1" in
remove)
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    ;;
purge)
    # 印刷履歴とテンプレートもここで消える
    rm -rf /var/lib/tepra-web
    if getent passwd tepra-web >/dev/null; then
        deluser --system tepra-web >/dev/null 2>&1 || true
    fi
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    ;;
esac

exit 0
EOF

chmod 0755 "$stage/DEBIAN/postinst" "$stage/DEBIAN/prerm" "$stage/DEBIAN/postrm"

mkdir -p "$outdir"
deb="$outdir/tepra-web_${version}_${arch}.deb"
fakeroot dpkg-deb --build "$stage" "$deb" >/dev/null
echo "$deb"
