"""OS-backed machine identity; local interface evidence for legacy recovery.

Neither installation directories nor interpreter-dependent uuid.getnode() identify
machines reliably. Raw OS identifiers stay local; only their digest is stored.
"""
import hashlib
import platform
import re
import subprocess
from functools import lru_cache
from pathlib import Path


def _read(command):
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          timeout=5).stdout


@lru_cache(maxsize=1)
def machine_id():
    system = platform.system()
    try:
        if system == 'Darwin':
            output = _read(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'])
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([A-Fa-f0-9-]+)"', output)
            value = match.group(1) if match else ''
        elif system == 'Linux':
            value = Path('/etc/machine-id').read_text().strip()
        elif system == 'Windows':
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r'SOFTWARE\Microsoft\Cryptography', 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value = winreg.QueryValueEx(key, 'MachineGuid')[0]
        else:
            raise ValueError(f'unsupported operating system: {system}')
        normalized = value.strip().lower().replace('-', '')
        if not re.fullmatch(r'[0-9a-f]{32}', normalized) or int(normalized, 16) == 0:
            raise ValueError('missing or invalid OS machine identifier')
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'Cannot establish stable host identity: {exc}') from exc
    return hashlib.sha256(f'plat-host-v2:{system}:{normalized}'.encode()).hexdigest()


def local_ids():
    """Only aliases independently derived from this machine, never caller input.

Legacy recovery cannot prove ownership after a hostname change or loss of the
original interface. Those cases deliberately require a separate handoff.
"""
    identities = {machine_id()}
    try:
        if platform.system() == 'Darwin':
            output = _read(['/sbin/ifconfig', '-a'])
            addresses = re.findall(r'\bether\s+([0-9a-fA-F:]{17})', output)
        elif platform.system() == 'Linux':
            addresses = [p.read_text().strip() for p in Path('/sys/class/net').glob('*/address')]
        else:
            addresses = []
        for address in addresses:
            number = int(address.replace(':', ''), 16)
            if number:
                identities.add(hashlib.sha256(f'{platform.node()}:{number}'.encode()).hexdigest())
    except (OSError, ValueError, subprocess.SubprocessError):
        # Stable identity remains usable when legacy interface evidence is absent.
        pass
    return identities
