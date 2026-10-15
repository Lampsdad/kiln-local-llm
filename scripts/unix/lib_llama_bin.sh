# shellcheck shell=bash
# ============================================================
#  Install a current llama.cpp build into ./llama-bin.
#
#  Sourced by install_fedora.sh, install_linux.sh and
#  install_mac.sh. The caller has already cd'd to the repo root.
#
#    kiln_install_llama_bin auto     NVIDIA: CUDA build matched to
#                                    the driver, plus the cudart
#                                    libs that tarball does not ship.
#                                    Otherwise Vulkan, then CPU.
#    kiln_install_llama_bin vulkan   Vulkan, then CPU. Not for WSL:
#                                    its Vulkan sees no GPU, so Vulkan
#                                    is the wrong package there.
#    kiln_install_llama_bin macos ARCH
#                                    arm64: the Metal build. Any
#                                    other ARCH gets the x64 build,
#                                    which upstream compiles without
#                                    Metal, so Intel runs on the CPU.
#
#  GitHub's /releases/latest is the v0.x stable line, which ships
#  no llama-server tarball. The nightly builds are still tagged
#  bNNNN. Same rule as scripts/windows/update.bat.
#
#  The CUDA tarball links libcudart and libcublas with RUNPATH
#  $ORIGIN and does not contain them. Fedora has no CUDA toolkit
#  by default, so those libs have to sit beside llama-server or
#  the binary loads and then cannot see the GPU. The matching
#  cudart-llama-*-bin-ubuntu-cuda-*-x64.tar.gz is that set.
#
#  The macOS tarball needs nothing extra. Its dylibs sit beside
#  llama-server, linked as @rpath/libggml-metal.0.dylib and so on
#  with @rpath = @loader_path, and the .0.dylib names are symlinks.
#  So the folder is copied whole with cp -a, which keeps them.
# ============================================================

kiln_install_llama_bin() {
    local mode="${1:-auto}" arch="${2:-}"
    if [ -f "llama-bin/llama-server" ]; then
        echo "[OK] llama-bin/ already populated, skipping download."
        return 0
    fi

    local py cuda=""
    # shellcheck source=lib_python.sh
    . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib_python.sh"
    py="$KILN_PY"
    if [ -z "$py" ]; then
        echo "ERROR: no working Python found. Install Python 3.8+ first."
        return 1
    fi

    # The driver's CUDA version, from nvidia-smi's header. Newer
    # nvidia-smi (615.65 here) prints "CUDA UMD Version: 13.4" where
    # it printed "CUDA Version: 13.4"; missing that sent every NVIDIA
    # card to Vulkan.
    if command -v nvidia-smi >/dev/null 2>&1; then
        cuda="$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA \(UMD \)\{0,1\}Version: \([0-9][0-9.]*\).*/\2/p' | head -1)"
    fi

    echo "[*] Fetching the newest llama.cpp build (bNNNN, not /releases/latest)..."
    local rel_json
    rel_json="$(curl -fsSL "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20")" || {
        echo "ERROR: could not reach the GitHub API."
        return 1
    }

    local plan
    plan="$(printf '%s' "$rel_json" | "$py" -c '
import json, re, sys
mode, cuda, arch = sys.argv[1], sys.argv[2], sys.argv[3]
rels = json.load(sys.stdin)
plat = "macos" if mode == "macos" else "ubuntu"

def ver(s):
    return tuple(int(x) for x in s.split(".") if x.isdigit())

for rel in rels:
    if not str(rel.get("tag_name", "")).startswith("b"):
        continue
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []}
    if not any(plat in n for n in assets):
        continue

    def find(pat):
        rx = re.compile(pat)
        hits = sorted(n for n in assets if rx.search(n))
        return hits[-1] if hits else ""

    def cuda_pair():
        # A 13.4 binary will not start on a driver that only speaks 13.0.
        # 12.8 is the oldest toolkit with Blackwell (sm_120) kernels.
        # Only accept a toolkit when the matching cudart tarball is
        # present too; otherwise try the next version, then Vulkan.
        prefer = "13.4" if cuda and ver(cuda) >= ver("13.4") else "12.8"
        order = [prefer] + [v for v in ("12.8", "13.4") if v != prefer]
        for v in order:
            name = find(r"^llama-b\d+-bin-ubuntu-cuda-" + re.escape(v) + r"-x64\.tar\.gz$")
            if not name:
                continue
            cudart = find(r"^cudart-llama-b\d+-bin-ubuntu-cuda-" + re.escape(v) + r"-x64\.tar\.gz$")
            if cudart:
                return name, cudart
        name = find(r"^llama-b\d+-bin-ubuntu-cuda-[\d.]+-x64\.tar\.gz$")
        if not name:
            return "", ""
        m = re.search(r"cuda-([\d.]+)-x64", name)
        cudart = find(r"^cudart-llama-b\d+-bin-ubuntu-cuda-" + re.escape(m.group(1)) + r"-x64\.tar\.gz$") if m else ""
        if not cudart:
            return "", ""
        return name, cudart

    kind, bin_name, cudart_name = "", "", ""
    if mode == "macos":
        # Only arm64 is built with Metal. The x64 tarball has no
        # libggml-metal, so an Intel Mac gets a CPU build.
        if arch == "arm64":
            bin_name, kind = find(r"^llama-b\d+-bin-macos-arm64\.tar\.gz$"), "metal"
        else:
            bin_name, kind = find(r"^llama-b\d+-bin-macos-x64\.tar\.gz$"), "cpu"
        if not bin_name:
            continue
    if mode == "auto" and cuda:
        bin_name, cudart_name = cuda_pair()
        kind = "cuda" if bin_name else ""
    if not bin_name and mode in ("auto", "vulkan"):
        bin_name = find(r"^llama-b\d+-bin-ubuntu-vulkan-x64\.tar\.gz$")
        kind = "vulkan" if bin_name else ""
        cudart_name = ""
    if not bin_name:
        bin_name = find(r"^llama-b\d+-bin-ubuntu-x64\.tar\.gz$")
        kind = "cpu" if bin_name else ""
        cudart_name = ""
    if not bin_name:
        continue
    print("TAG=" + rel["tag_name"])
    print("KIND=" + kind)
    print("BIN=" + assets[bin_name])
    if cudart_name:
        print("CUDART=" + assets[cudart_name])
    raise SystemExit(0)
sys.exit(1)
' "$mode" "$cuda" "$arch")" || {
        if [ "$mode" = macos ]; then
            echo "ERROR: no macOS $arch llama.cpp build in the newest releases."
        else
            echo "ERROR: no Ubuntu x64 llama.cpp build in the newest releases."
        fi
        echo "       https://github.com/ggml-org/llama.cpp/releases"
        return 1
    }

    local tag="" kind="" bin_url="" cudart_url="" line k v
    while IFS='=' read -r k v; do
        case "$k" in
            TAG)    tag="$v" ;;
            KIND)   kind="$v" ;;
            BIN)    bin_url="$v" ;;
            CUDART) cudart_url="$v" ;;
        esac
    done <<EOF
$plan
EOF

    echo "[*] Build $tag ($kind)"
    local stage
    stage="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$stage'" RETURN

    echo "[*] Downloading $(basename "$bin_url")..."
    curl -fL "$bin_url" -o "$stage/bin.tgz"
    mkdir -p "$stage/bin"
    tar -xzf "$stage/bin.tgz" -C "$stage/bin"

    if [ -n "$cudart_url" ]; then
        echo "[*] Downloading $(basename "$cudart_url") (CUDA runtime, not in the server tarball)..."
        curl -fL "$cudart_url" -o "$stage/cudart.tgz"
        mkdir -p "$stage/cudart"
        tar -xzf "$stage/cudart.tgz" -C "$stage/cudart"
    elif [ "$kind" = "cuda" ]; then
        echo "ERROR: this CUDA build needs the matching cudart tarball and it was not in the release."
        return 1
    fi

    local server dest
    server="$(find "$stage/bin" -type f -name llama-server | head -1)"
    if [ -z "$server" ]; then
        echo "ERROR: llama-server was not in $(basename "$bin_url")."
        return 1
    fi
    dest="$stage/out"
    mkdir -p "$dest"
    cp -a "$(dirname "$server")/." "$dest/"
    if [ -d "$stage/cudart" ]; then
        find "$stage/cudart" -type f -name 'lib*.so*' -exec cp -a {} "$dest/" \;
    fi
    chmod +x "$dest/llama-server" "$dest/llama-cli" 2>/dev/null || true

    if [ "$kind" = "cuda" ]; then
        if ldd "$dest/libggml-cuda.so" 2>/dev/null | grep -q 'not found'; then
            echo "ERROR: CUDA libraries are still missing next to llama-server:"
            ldd "$dest/libggml-cuda.so" | grep 'not found' || true
            return 1
        fi
    fi

    rm -rf llama-bin
    mv "$dest" llama-bin
    echo "[OK] llama.cpp $tag ($kind) installed to llama-bin/."
    if ! ./llama-bin/llama-server --help 2>&1 | grep -q 'draft-mtp'; then
        echo "WARNING: this build does not advertise draft-mtp. Qwen3.8 MTP will be off."
    fi
}
