# Avatar model

The AI session tab renders a VRM avatar that lip-syncs to the spoken turns.
The model itself is **not** in this repo — drop your own here as:

    static/models/avatar.glb

Without it the rest of the app works normally; only the avatar panel stays
empty.

## Why it is not committed

Not size — licensing. VRM files carry their own terms in their metadata, and
VRoid Hub models are commonly published with `redistribution=disallow`. This
repository is public, so committing such a model would redistribute it.

Before adding a model here, check what its licence actually permits. The terms
live in the file itself and can be read without any special tooling:

```python
import json, struct
with open("static/models/avatar.glb", "rb") as f:
    struct.unpack("<III", f.read(12))          # header
    clen, _ = struct.unpack("<II", f.read(8))  # JSON chunk
    meta = json.loads(f.read(clen))["extensions"]["VRM"]["meta"]
print(meta["licenseName"], meta.get("otherLicenseUrl"))
```

## Requirements

Any VRM works, but for lip-sync the model needs the five standard mouth
expressions — `aa`, `ih`, `ou`, `ee`, `oh`. VRoid exports these as
`Fcl_MTH_A/I/U/E/O` and three-vrm normalises the names on load. A model
without them still renders and blinks; its mouth just will not move.

Mouth openness is tuned per-model in `static/js/avatar.js` via `VISEME_GAIN`
and the per-shape `VISEME_SCALE` table, since the morphs are not equally
strong between models.
