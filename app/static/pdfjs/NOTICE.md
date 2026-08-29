# pdf.js

`pdf.min.js` and `pdf.worker.min.js` are Mozilla's PDF.js, version **3.11.174**, taken
from `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/`. Copyright 2023 Mozilla
Foundation, licensed under the Apache License 2.0 — the full notice is in the header of
each file.

## Why these are in the repository

The notes viewer (`app/routers/notes.py`) exists so that a chapter's PDF is read through a
page we serve rather than by handing the app a storage URL. Loading the renderer for that
page from a third-party CDN would have put a second host in the way of something that
already requires this API to be reachable — the PDF itself streams through here — so the
CDN could only ever *reduce* the chance the page works, never improve it. It is also one
more thing to get wrong: the first version of this page carried a fabricated integrity
hash, the browser refused the script, and the viewer sat on "Opening the notes…" forever.

## Updating

Download both files at the same version, replace them here, and update the version above.
Nothing in our code is version-specific beyond `GlobalWorkerOptions.workerSrc` pointing at
the worker route.
