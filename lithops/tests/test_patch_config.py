import yaml

from lithops.config import patch_config


def write_yaml(tmp_path, content_dict):
    p = tmp_path / 'conf.yml'
    with open(p, 'w') as f:
        yaml.safe_dump(content_dict, f)
    return str(p)


def test_patch_config_merges_and_preserves(tmp_path):
    base = {
        'lithops': {
            'mode': 'localhost',
            'backend': 'localhost',
            'storage': 'localhost',
            'log_level': 'INFO'
        },
        'section': {
            'nested': {
                'a': 1,
                'b': 2
            },
            'keep': True
        }
    }
    cfg_file = write_yaml(tmp_path, base)

    patch = {
        'lithops': {'log_level': 'DEBUG'},
        'section': {
            'nested': {
                'b': 999,  # override existing
                'c': 3     # add new
            },
            'new_key': 'value'
        },
        'new_section': {'x': 42}
    }

    patched = patch_config(cfg_file, patch)

    # Scalars overridden
    assert patched['lithops']['log_level'] == 'DEBUG'
    # Unchanged scalars preserved
    assert patched['lithops']['mode'] == 'localhost'
    # Deep merge: existing key overridden, new key added, untouched key kept
    assert patched['section']['nested']['b'] == 999
    assert patched['section']['nested']['c'] == 3
    assert patched['section']['keep'] is True
    assert patched['section']['new_key'] == 'value'
    # Completely new section added
    assert patched['new_section']['x'] == 42
