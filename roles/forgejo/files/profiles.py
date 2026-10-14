"""Reviewed exact runtime profiles; archives cannot choose executable images."""

IMAGE_PATTERN = r'codeberg[.]org/forgejo/forgejo:(?:15|16)[.]\d+[.]\d+-rootless@sha256:[0-9a-f]{64}'
KEYS = ('image', 'postgres_image', 'rclone_image')
DATABASE = 'docker.io/library/postgres:17.11@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f'
RCLONE = 'docker.io/rclone/rclone:1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5'
REVIEWED = {
    'forgejo-15.0.9': {
        'image': 'codeberg.org/forgejo/forgejo:15.0.9-rootless@sha256:caf1bca332f95cdcf124227a4bfa3b49bbbfbbc8a5e4a97406921cb581165413',
        'postgres_image': DATABASE, 'rclone_image': RCLONE,
    },
    'forgejo-16.0.6': {
        'image': 'codeberg.org/forgejo/forgejo:16.0.6-rootless@sha256:51b47921660e1ec7127f094208fe06901ba57df4fe6d59fcccc3a2419514a77f',
        'postgres_image': DATABASE, 'rclone_image': RCLONE,
    },
}


def recovery_pins(manifest: dict, active: dict) -> dict:
    pins = {key: manifest[key] for key in KEYS}
    if pins == {key: active[key] for key in KEYS} or pins in REVIEWED.values():
        return pins
    raise ValueError('archive has no reviewed exact recovery profile')
