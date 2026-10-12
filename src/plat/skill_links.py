"""Portable packaged workflows; never overwrite an unrelated skill."""
from pathlib import Path


def install(provider='all', unlink=False):
    if provider not in ('all', 'claude', 'codex'):
        raise ValueError('provider must be all, claude, or codex')
    source = Path(__file__).parent / 'bundled_skills'
    if not source.is_dir():
        source = Path(__file__).resolve().parents[2] / 'skills'
    if not source.is_dir():
        raise ValueError('this installation is missing its bundled skills')
    output = []
    for name in ('claude', 'codex') if provider == 'all' else (provider,):
        target = Path.home() / ('.claude' if name == 'claude' else '.agents') / 'skills'
        target.mkdir(parents=True, exist_ok=True)
        for skill in sorted(source.iterdir()):
            if not (skill / 'SKILL.md').is_file():
                continue
            link = target / skill.name
            if link.is_symlink() and link.resolve() == skill.resolve():
                if unlink:
                    link.unlink()
                    output.append(f'{name}: unlinked {skill.name}')
                else:
                    output.append(f'{name}: already linked {skill.name}')
            elif link.exists() or link.is_symlink():
                output.append(f'{name}: skipped existing {skill.name}')
            elif not unlink:
                link.symlink_to(skill.resolve())
                output.append(f'{name}: linked {skill.name}')
    return output
