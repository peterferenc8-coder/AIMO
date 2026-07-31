# Avatar models

The AI session tab renders a VRM avatar that lip-syncs to the spoken turns.

`public.vrm` ships as the default and is the only model committed here. Pick a
different one — or add your own — under **Settings → Avatar**.

## Where models live

| Source | Location | Notes |
| --- | --- | --- |
| Bundled | `static/models/` | Read-only, shipped inside the release binary. Listed as `builtin:<name>`. |
| Uploaded | `DATA_DIR/models/` (`~/.config/aimee/models`) | Written by **Settings → Avatar**. Listed as `user:<name>`. |

Uploads deliberately go to `DATA_DIR`, not here. In a PyInstaller build this
folder is unpacked into a temp directory that is wiped on every run, so a model
written into it would not survive a restart — and dropping a file in by hand is
not possible at all from a release binary. Uploading is the only route for
users who are not running from source.

Supported extensions are `.vrm` and `.glb`; anything else is rejected on upload.

## Adding a model to the bundle

Committing a model here means redistributing it, so check the licence first.
VRM terms live in the file's own metadata and can be read without any tooling:

```python
import json, struct
with open("static/models/public.vrm", "rb") as f:
    struct.unpack("<III", f.read(12))          # header
    clen, _ = struct.unpack("<II", f.read(8))  # JSON chunk
    meta = json.loads(f.read(clen))["extensions"]["VRM"]["meta"]
print(meta["licenseName"], meta.get("otherLicenseUrl"))
```

The `otherLicenseUrl` query string carries the actual permissions. **It must say
`redistribution=allow`** — VRoid Hub models are commonly published with
`redistribution=disallow`, which rules out shipping them in a public repo or a
release binary. `.gitignore` ignores this folder by default for exactly that
reason, with a named exception for `public.vrm`; add another `!` line only after
confirming the licence permits it.

Models that cannot be redistributed are still perfectly usable — upload them
through Settings instead, where they stay on the local machine.

## Requirements

Any VRM works, but for lip-sync the model needs the five standard mouth
expressions — `aa`, `ih`, `ou`, `ee`, `oh`. VRoid exports these as
`Fcl_MTH_A/I/U/E/O` and three-vrm normalises the names on load. A model
without them still renders and blinks; its mouth just will not move.

Mouth openness is tuned in `static/js/avatar.js` via `VISEME_GAIN` and the
per-shape `VISEME_SCALE` table. These are global, not per-model, and the morphs
are not equally strong between models — so a model whose mouth reads as too
slack or too tight after switching needs those constants adjusting rather than
being a fault in the model.
