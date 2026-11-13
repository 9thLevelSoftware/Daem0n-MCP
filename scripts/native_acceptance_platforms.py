"""Run isolated native-client acceptance preparation on WSL and the paired Mac.

Each invocation saves a new immutable evidence attempt. This is a transport and
preparation helper, not evidence that a native model workflow has passed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = REPOSITORY / ".test_tmp/native-followup-20261112/platforms"
REMOTE_DIRECTORY = ".daem0nmcp-native-acceptance/20261112"
CLIENT_PACKAGES = {
    "claude": "@anthropic-ai/claude-code@2.1.274",
    "codex": "@openai/codex@0.167.0",
    "opencode": "opencode-ai@1.18.21",
    "pi": "@earendil-works/pi-coding-agent@1.0.0",
}


def redact(value: str) -> str:
    value = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
        "<REDACTED_JWT>",
        value,
    )
    value = re.sub(
        r"(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_-]{12,}|Bearer\s+[^\s\"']+",
        "<REDACTED_CREDENTIAL>",
        value,
    )
    return re.sub(
        r'("(?:access_?token|refresh_?token|id_?token|api_?key|authorization)"\s*:\s*)'
        r'"(?:[^"\\]|\\.)*"',
        r'\1"<REDACTED_AUTH_FIELD>"',
        value,
        flags=re.IGNORECASE,
    )


def transport(platform_name: str) -> list[str]:
    windows = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"
    if platform_name == "linux":
        return [str(windows / "wsl.exe"), "-d", "Ubuntu", "--", "/usr/bin/python3", "-"]
    return [
        str(windows / "OpenSSH/ssh.exe"),
        "-i",
        str(Path.home() / ".ssh/daem0n_acceptance_mac"),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "KexAlgorithms=curve25519-sha256",
        "-o",
        "ConnectTimeout=10",
        "christopherwilloughby@192.168.0.158",
        "/opt/homebrew/bin/python3 -",
    ]


def prelude() -> str:
    return "\n".join(
        [
            "import hashlib,json,os,pathlib,platform,shutil,stat,subprocess,sys,time",
            "original_home = pathlib.Path.home()",
            f"root = original_home / {REMOTE_DIRECTORY!r}",
            "root.mkdir(parents=True, exist_ok=True, mode=0o700)",
            "path_parts = [str(root/'tools/bin'), str(root/'node/bin'),"
            " '/opt/homebrew/bin','/usr/local/bin',str(original_home/'.local/bin'),"
            " '/usr/bin','/bin','/usr/sbin','/sbin']",
            "os.environ['PATH'] = ':'.join(path_parts)",
            "def clean_environment(home):",
            "    home.mkdir(parents=True, exist_ok=True, mode=0o700)",
            "    result = {'PATH':os.environ['PATH'], 'HOME':str(home),"
            " 'XDG_CONFIG_HOME':str(home/'.config'), 'XDG_DATA_HOME':str(home/'.local/share'),"
            " 'XDG_CACHE_HOME':str(home/'.cache'), 'XDG_STATE_HOME':str(home/'.local/state'),"
            " 'LANG':'en_US.UTF-8', 'LC_ALL':'en_US.UTF-8', 'NO_COLOR':'1'}",
            "    for name in ('SSH_CONNECTION','SSH_CLIENT','SSH_TTY'):",
            "        if name in os.environ: result[name] = os.environ[name]",
            "    return result",
            "def fingerprint(path):",
            "    if not path.is_file(): return None",
            "    return {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),"
            " 'size':path.stat().st_size, 'mode':oct(stat.S_IMODE(path.stat().st_mode))}",
        ]
    )


def inspect_script() -> str:
    return (
        prelude()
        + r"""
configuration_paths = [
    '.codex/config.toml','.codex/auth.json','.codex/hooks.json',
    '.claude.json','.claude/settings.json','.claude/.credentials.json',
    '.gemini/config/mcp_config.json','.gemini/config/hooks.json',
    '.gemini/antigravity-cli/settings.json','.gemini/antigravity/app_storage.json',
    '.config/opencode/opencode.json','.local/share/opencode/auth.json',
    '.omp/agent/agent.db','.omp/agent/mcp.json','.pi/agent/auth.json','.pi/agent/mcp.json',
]
before = {name:fingerprint(original_home/name) for name in configuration_paths}
clients = {}
for client in ('claude','codex','agy','opencode','omp','pi'):
    binary = shutil.which(client)
    item = {'binary':binary,'installed':binary is not None}
    if binary:
        result = subprocess.run([binary,'--version'], cwd=root,
            env=clean_environment(root/'private/version-probes'/client),
            capture_output=True,text=True,timeout=45)
        item.update(exit_code=result.returncode,version_stdout=result.stdout.strip(),
            version_stderr=result.stderr.strip())
    clients[client] = item
after = {name:fingerprint(original_home/name) for name in configuration_paths}
print(json.dumps({'platform':platform.platform(),'system':platform.system(),
    'machine':platform.machine(),'python':sys.version,'python_executable':sys.executable,
    'root':str(root),'clients':clients,'tools':{name:shutil.which(name)
        for name in ('git','python3','uv','node','npm','curl','tar')},
    'original_configuration_before':before,'original_configuration_after':after,
    'changed_original_files':[name for name in before if before[name]!=after[name]],
    'authentication':'Presence metadata only; no account/token contents inspected',
    'native_model_calls':0},indent=2))
"""
    )


def prepare_script() -> str:
    # uv and Node are installed into the owned test root, never a global prefix.
    return (
        prelude()
        + r"""
environment = clean_environment(root/'private/preparation')
phases = []
def command(label, argv, timeout=600):
    result = subprocess.run(argv,cwd=root,env=environment,capture_output=True,text=True,timeout=timeout)
    item={'phase':label,'argv':argv,'exit_code':result.returncode,
        'stdout':result.stdout[-12000:],'stderr':result.stderr[-12000:]}
    phases.append(item)
    print(json.dumps(item),flush=True)
    if result.returncode: raise SystemExit(result.returncode)
    return result.stdout
uv = shutil.which('uv')
if not uv:
    # The pinned wheel contains the platform-native executable and is installed
    # through system Python into a project-owned target, with no global writes.
    target = root/'uv-package'
    command('install-pinned-uv',[sys.executable,'-m','pip','install','--target',str(target),
        '--no-deps','uv==0.11.12'])
    candidates = list(target.glob('uv/uv')) + list(target.glob('bin/uv'))
    uv = str(next(path for path in candidates if path.is_file()))
command('python-3.12',[uv,'python','install','3.12','--install-dir',str(root/'python')])
python_candidates = sorted((root/'python').glob('cpython-3.12.*/bin/python3.12'))
python = str(python_candidates[0])
if not (root/'venv/bin/python').is_file():
    command('create-python-environment',[uv,'venv','--python',python,str(root/'venv')])
command('python-version',[str(root/'venv/bin/python'),'--version'])
print(json.dumps({'prepared':True,'root':str(root),'python':str(root/'venv/bin/python'),
    'uv':uv,'native_model_calls':0,'source_not_staged':True}),flush=True)
"""
    )


def clients_script() -> str:
    return (
        prelude()
        + "\npackages = "
        + repr(list(CLIENT_PACKAGES.values()))
        + r"""
import io,tarfile,urllib.request
environment = clean_environment(root/'private/client-installation')
prefix = root/'tools'
(prefix/'bin').mkdir(parents=True,exist_ok=True)
def command(label,argv,timeout=600):
    result=subprocess.run(argv,cwd=root,env=environment,capture_output=True,text=True,timeout=timeout)
    print(json.dumps({'phase':label,'argv':argv,'exit_code':result.returncode,
        'stdout':result.stdout[-12000:],'stderr':result.stderr[-12000:]}),flush=True)
    if result.returncode: raise SystemExit(result.returncode)
    return result.stdout
npm = shutil.which('npm')
if not npm: raise RuntimeError('Native npm prerequisite missing')
command('node-version',[shutil.which('node'),'--version'])
installed = []
for package in packages:
    name,version = package.rsplit('@',1)
    package_json=prefix/'lib/node_modules'/name/'package.json'
    installed.append(package_json.is_file() and json.loads(package_json.read_text())['version']==version)
if not all(installed):
    command('install-pinned-npm-clients',[npm,'install','--global','--prefix',str(prefix),
        '--no-audit','--no-fund',*packages],1200)
# Recent npm requires explicit per-command permission for dependency scripts.
command('run-reviewed-install-scripts',[npm,'rebuild','--global','--prefix',str(prefix),
    '--allow-scripts=@anthropic-ai/claude-code,opencode-ai,@google/genai,esbuild,protobufjs'],600)
system = platform.system()
machine = platform.machine()
omp_assets = {
    ('Darwin','arm64'):('omp-darwin-arm64','d4a10d97488d6054d2b18198bd24f461079952c7e0fb75ba66336ca0df0cb99e'),
    ('Linux','x86_64'):('omp-linux-x64','6498ed785bab00d2926e0387e0ec02362d6d45f1c3d0d1b57c1618b7f95c9264'),
}
asset,digest = omp_assets[(system,machine)]
url = 'https://github.com/can1357/oh-my-pi/releases/download/v18.16.0/'+asset
data = urllib.request.urlopen(url,timeout=90).read(300_000_000)
if hashlib.sha256(data).hexdigest()!=digest: raise RuntimeError('OMP release digest mismatch')
omp = prefix/'bin/omp'
if omp.exists():
    if hashlib.sha256(omp.read_bytes()).hexdigest()!=digest: raise RuntimeError('Existing private OMP differs')
else:
    omp.write_bytes(data)
    omp.chmod(0o700)
print(json.dumps({'phase':'install-pinned-omp','url':url,'sha256':digest,'bytes':len(data)}),flush=True)
agy_releases={
    'Darwin':('darwin-arm/cli_mac_arm64.tar.gz','c9746409083a9b15c217757aa2ef399ee7d4398602a30038583ea45196fc27d1662745341e45a116c07be19c08587de1e5929e365d2a46aea385fa33f0c2396a'),
    'Linux':('linux-x64/cli_linux_x64.tar.gz','60bca280414ed11723f58b3b671962ae394cc2d0c25e3f58e32e1c6847c978cbc77ec6f40e7f3992001c399d80aeb8b64c9ec892abddd591b12413c5ee6d9fce'),
}
artifact,digest=agy_releases[system]
manifest={'version':'1.5.7','url':'https://storage.googleapis.com/antigravity-public/antigravity-cli/1.5.7-6190022064472064/'+artifact,'sha512':digest}
data=urllib.request.urlopen(manifest['url'],timeout=90).read(300_000_000)
if hashlib.sha512(data).hexdigest()!=manifest['sha512']: raise RuntimeError('AGY release digest mismatch')
if '.tar.gz' in manifest['url']:
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
        entry=archive.getmember('antigravity')
        if not entry.isfile(): raise RuntimeError('AGY payload is not a regular file')
        data=archive.extractfile(entry).read(300_000_000)
agy=prefix/'bin/agy'
if agy.exists(): raise RuntimeError('AGY private target already exists; preserve it')
agy.write_bytes(data)
agy.chmod(0o700)
print(json.dumps({'phase':'install-pinned-agy','manifest':manifest,'binary_sha256':hashlib.sha256(data).hexdigest()}),flush=True)
for client in ('claude','codex','agy','opencode','omp','pi'):
    binary = prefix/'bin'/client
    command(client+'-version',[str(binary),'--version'],90)
print(json.dumps({'clients_prepared':True,'prefix':str(prefix),'native_model_calls':0}),flush=True)
"""
    )


def stage_script(tree: Path, destination: str) -> str:
    """Stage an explicitly selected frozen tree without archive extraction paths."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+(?:/[a-zA-Z0-9_-]+)*", destination):
        raise ValueError("remote destination must contain only simple relative names")
    tree = tree.resolve(strict=True)
    archive_bytes = io.BytesIO()
    manifest: dict[str, str] = {}
    total = 0
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        for path in sorted(tree.rglob("*")):
            if path.is_symlink():
                raise ValueError("frozen source must not contain links")
            if not path.is_file():
                continue
            relative = path.relative_to(tree).as_posix()
            if any(
                part in {".git", ".venv", "node_modules", ".daem0nmcp"}
                for part in path.relative_to(tree).parts
            ):
                raise ValueError(
                    "frozen source contains configuration or runtime state"
                )
            data = path.read_bytes()
            total += len(data)
            if total > 128 * 1024 * 1024:
                raise ValueError("frozen source exceeds its 128 MiB bound")
            entry = tarfile.TarInfo(relative)
            entry.size = len(data)
            entry.mode = 0o600
            archive.addfile(entry, io.BytesIO(data))
            manifest[relative] = hashlib.sha256(data).hexdigest()
    encoded = base64.b64encode(archive_bytes.getvalue()).decode("ascii")
    return (
        prelude()
        + "\n"
        + "\n".join(
            [
                "import base64,io,tarfile",
                f"destination = root / {destination!r}",
                "destination.mkdir(parents=True, exist_ok=False, mode=0o700)",
                f"expected = {manifest!r}",
                f"payload = base64.b64decode({encoded!r},validate=True)",
                "with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:",
                "    for entry in archive:",
                "        if not entry.isfile() or entry.name not in expected: raise RuntimeError('Invalid source archive entry')",
                "        data = archive.extractfile(entry).read()",
                "        if hashlib.sha256(data).hexdigest()!=expected[entry.name]: raise RuntimeError('Source archive digest mismatch')",
                "        target = destination/entry.name",
                "        target.parent.mkdir(parents=True,exist_ok=True)",
                "        target.write_bytes(data)",
                "print(json.dumps({'staged':str(destination),'file_count':len(expected),'source_hashes':expected,'native_model_calls':0},indent=2))",
            ]
        )
    )


def v2_script() -> str:
    return (
        prelude()
        + r"""
environment=clean_environment(root/'private/opencode-v2-preparation')
prefix=root/'tools-v2'
argv=[shutil.which('npm'),'install','--global','--prefix',str(prefix),
    '--no-audit','--no-fund','--allow-scripts=@opencode/cli','@opencode/cli@2.2.7']
result=subprocess.run(argv,cwd=root,env=environment,capture_output=True,text=True,timeout=600)
print(json.dumps({'phase':'install-pinned-opencode-v2','argv':argv,'exit_code':result.returncode,
    'stdout':result.stdout[-12000:],'stderr':result.stderr[-12000:]}),flush=True)
if result.returncode: raise SystemExit(result.returncode)
binary=prefix/'bin/opencode2'
for label,arguments in [('version',['--version']),('help',['--help'])]:
    result=subprocess.run([str(binary),*arguments],cwd=root,env=environment,capture_output=True,text=True,timeout=60)
    print(json.dumps({'phase':'opencode-v2-'+label,'exit_code':result.returncode,
        'stdout':result.stdout,'stderr':result.stderr}),flush=True)
    if result.returncode: raise SystemExit(result.returncode)
print(json.dumps({'opencode_v2_prepared':True,'version':'2.2.7','binary':str(binary),
    'native_model_calls':0}),flush=True)
"""
    )


def claude_enforcement_script() -> str:
    return (
        prelude()
        + r"""
environment=clean_environment(root/'private/claude-enforcement-preparation')
prefix=root/'tools-claude-enforce'
argv=[shutil.which('npm'),'install','--global','--prefix',str(prefix),
    '--no-audit','--no-fund','--allow-scripts=@anthropic-ai/claude-code','@anthropic-ai/claude-code@2.1.295']
result=subprocess.run(argv,cwd=root,env=environment,capture_output=True,text=True,timeout=600)
print(json.dumps({'phase':'install-pinned-claude-enforcement-client','argv':argv,'exit_code':result.returncode,
    'stdout':result.stdout[-12000:],'stderr':result.stderr[-12000:]}),flush=True)
if result.returncode: raise SystemExit(result.returncode)
binary=prefix/'bin/claude'
result=subprocess.run([str(binary),'--version'],cwd=root,env=environment,capture_output=True,text=True,timeout=60)
print(json.dumps({'phase':'claude-enforcement-client-version','exit_code':result.returncode,
    'stdout':result.stdout,'stderr':result.stderr}),flush=True)
if result.returncode: raise SystemExit(result.returncode)
"""
    )


def execute(
    args: argparse.Namespace,
    script: str,
    *,
    sensitive_values: tuple[str, ...] = (),
) -> dict[str, object]:
    attempt = args.evidence / args.platform / args.attempt
    attempt.mkdir(parents=True, exist_ok=False)
    command = transport(args.platform)
    environment = dict(os.environ)
    # Avoid WSL importing stale Windows drive paths during distribution startup.
    if args.platform == "linux":
        environment["PATH"] = str(
            Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32"
        )
    started = time.monotonic()
    (attempt / "request.json").write_text(
        json.dumps(
            {
                "argv": command,
                "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                "action": args.action,
                "platform": args.platform,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            command,
            input=script,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout,
        )
        stdout, stderr, code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        stderr += "\nPlatform operation exceeded its configured deadline.\n"
        code = 124
    for value in sensitive_values:
        if value:
            stdout = stdout.replace(value, "<REDACTED_SELECTED_CREDENTIAL>")
            stderr = stderr.replace(value, "<REDACTED_SELECTED_CREDENTIAL>")
    (attempt / "stdout.log").write_text(redact(stdout), encoding="utf-8")
    (attempt / "stderr.log").write_text(redact(stderr), encoding="utf-8")
    summary = {
        "action": args.action,
        "platform": args.platform,
        "exit_code": code,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "evidence": str(attempt),
        "native_workflow_accepted": False,
    }
    (attempt / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "inspect",
            "prepare",
            "clients",
            "clients-v2",
            "claude-enforce",
            "stage",
            "script",
        ),
    )
    parser.add_argument("--platform", choices=("linux", "macos"), required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--tree", type=Path)
    parser.add_argument("--destination")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}", args.attempt):
        parser.error("attempt must be a short filename-safe identifier")
    if args.action == "stage":
        if args.tree is None or args.destination is None:
            parser.error("stage requires --tree and --destination")
        script = stage_script(args.tree, args.destination)
    elif args.action == "script":
        if args.script is None:
            parser.error("script action requires --script")
        script = prelude() + "\n" + args.script.read_text(encoding="utf-8")
    else:
        script = {
            "inspect": inspect_script,
            "prepare": prepare_script,
            "clients": clients_script,
            "clients-v2": v2_script,
            "claude-enforce": claude_enforcement_script,
        }[args.action]()
    result = execute(args, script)
    print(json.dumps(result, indent=2))
    return int(result["exit_code"])


if __name__ == "__main__":
    sys.exit(main())
