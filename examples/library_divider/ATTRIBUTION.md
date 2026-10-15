# Official KiCad library fixture attribution

The files in `libraries/` are KiCad library data compiled by the **KiCad community**. They retain the **Creative Commons Attribution-ShareAlike 4.0 International license with the KiCad Libraries exception**. They are **not covered by this repository's MIT software license**.

The official upstream release is **9.0.0**. The annotated release tags were resolved to these exact repository commits:

| Repository | Exact commit |
| --- | --- |
| [KiCad Symbols](https://gitlab.com/kicad/libraries/kicad-symbols) | `a9012d8971ef2ea3243ffc9f19a5b4f153d8b334` |
| [KiCad Footprints](https://gitlab.com/kicad/libraries/kicad-footprints) | `e515f1c3213317d436e492c8c06620ef4caca84e` |

`Device.kicad_sym` contains only the upstream `R` symbol. `Connector_Generic.kicad_sym` contains only the upstream `Conn_01x02` symbol. These are derived subsets: the library headers and each selected symbol's text were retained; all other top-level symbols were omitted. No pin type, number, name, coordinate, graphical primitive or selected symbol property was changed. The `.kicad_mod` footprint files are **unmodified upstream bytes**.

[SOURCE-PROVENANCE.json](SOURCE-PROVENANCE.json) records each original path, exact raw download URL, commit, original complete file SHA-256, vendored file SHA-256 and derivation. `registry.json` pins the **actual vendored bytes** used by the generator. Upstream SHA-256 values for the complete symbol libraries therefore differ from the deliberately reduced local symbol files.

The original upstream [KiCad Libraries License](KICAD-LIBRARIES-LICENSE.md) is retained verbatim; its exception and attribution apply to these data. The full [CC-BY-SA 4.0 legal text](CC-BY-SA-4.0.txt) is copied from the official KiCad source repository, release tag `9.0.9`; its original URL and hash are recorded in the provenance file. The symbol and footprint repositories contain identical `LICENSE.md` bytes at their pinned commits.

The [official KiCad library licensing page](https://www.kicad.org/libraries/license/) explains the design-output exception and the continued attribution/share-alike requirements when redistributing library collections. This fixture redistributes a small library collection, so the license and attribution accompany it and any copied build source snapshots.

This circuit exercises library loading, real pad geometry, electrical pin types, net mapping and rotation. Library inclusion does not establish a component MPN, datasheet pinout, board routability or manufacturing qualification. The KiCad community supplies its libraries without warranty.
