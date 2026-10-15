# Génération avec bibliothèques KiCad épinglées

Le parcours `--bindings` utilise de vraies définitions de symboles et d'empreintes KiCad locales, vérifiées par SHA-256. Les broches conservent leur type électrique et les pads conservent leur géométrie, leurs perçages et leurs couches. Il produit un **brouillon de bibliothèque** (`kind: library-draft`), avec `fabrication_ready: false`, `routed: false` et `qualified: false`.

Cette étape remplace les formes génériques du mode aperçu pour les composants explicitement liés. Elle ne prouve ni le choix d'un MPN, ni le pinout d'une datasheet, ni le routage ou la conformité fabricant. Sans `--bindings`, `build` conserve le parcours aperçu existant.

## Essayer le diviseur de référence

Depuis la racine du dépôt, avec `pcb-dev` installé :

```sh
pcb-dev check examples/library_divider/design.json
pcb-dev build examples/library_divider/design.json \
  --bindings examples/library_divider/bindings.json \
  --output build/library-divider
pcb-dev verify build/library-divider
```

Le répertoire de sortie doit être nouveau. Aucun téléchargement ne fait partie du chargement des bindings ou de la génération. Les sources locales épinglées, la provenance et les documents de licence sont conservés avec le build pour la revue et l'archivage.

Le fixture contient un connecteur et deux résistances reliés par trois nets globaux :

| Net | Broches déclarées |
| --- | --- |
| `VIN` | `J1.1`, `R1.1` |
| `VOUT` | `R1.2`, `R2.1` |
| `GND` | `R2.2`, `J1.2` |

`J1` utilise le symbole `Connector_Generic:Conn_01x02` et l'empreinte traversante `Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical`. `R1` et `R2` utilisent `Device:R` et l'empreinte CMS `Resistor_SMD:R_0603_1608Metric`. Les résistances valent `10k` dans le modèle ; ce choix illustre l'équation idéale d'un diviseur, sans sélection d'une référence fabricant. Le rapport des paramètres calcule `vout_v = 2.5` pour `vin_v = 5`.

Les positions de test sont explicites, sur la face supérieure : `J1` à `(10,15)` mm et `90°`, `R1` à `(35,20)` mm et `90°`, `R2` à `(55,35)` mm et `270°`. Ces rotations servent à détecter les erreurs de transformation et d'orientation des pads.

## Écrire les bindings

Un fichier de bindings déclare un registre local et une entrée par chemin de composant, y compris pour les composants non montés :

```json
{
  "schema_version": 1,
  "registry": "registry.json",
  "licenses": ["ATTRIBUTION.md", "KICAD-LIBRARIES-LICENSE.md", "CC-BY-SA-4.0.txt", "SOURCE-PROVENANCE.json"],
  "components": {
    "board.r1": {
      "symbol_library": "symbol-device",
      "symbol_name": "R",
      "symbol_id": "Device:R",
      "footprint_library": "footprint-r0603",
      "unit": 1,
      "no_connect_pins": []
    }
  }
}
```

Ce fragment montre une entrée ; le fixture complet contient également `board.j1` et `board.r2`. `component.footprint` donne l'identifiant d'empreinte complet. Son nom après `:` doit correspondre au nom de l'empreinte source. Les alias du registre pointent vers des fichiers relatifs contenus dans son répertoire et leurs octets exacts sont épinglés par SHA-256.

Le chargement refuse les champs inconnus, bindings absents ou supplémentaires, traversées de chemins, liens sortant de leur périmètre, sources modifiées et transformations non prises en charge. Il n'utilise aucun composant générique de secours. Les définitions de symboles hérités sont résolues localement avant génération ; un héritage manquant ou incompatible bloque le chargement.

Chaque numéro de broche doit figurer dans `component.pins` ou dans `no_connect_pins`, sans ambiguïté. Les pads électriques numérotés de l'empreinte doivent couvrir exactement les broches. La portée initiale est un symbole à une unité (`unit=1`, graphiques communs `unit=0`, conversions `0/1`) et un placement sur la face supérieure. Les symboles multi-unités, styles alternatifs non pris en charge ou placements sur la face inférieure sont refusés explicitement.

Les marqueurs de non-connexion explicites sont émis, mais ce fixture nominal n'en contient pas. Les nets de substitution que KiCad peut produire pour une broche non connectée exigent une comparaison de parité adaptée aux bindings ; ce fixture ne constitue pas une validation native de ce cas.

## Fichiers générés et portée des preuves

Le schéma embarque les définitions réelles des symboles. Le PCB contient les pads, perçages, dessins de fabrication, sérigraphie et courtyards des empreintes liées. Des tables et bibliothèques locales permettent à KiCad de résoudre les mêmes définitions épinglées, indépendamment des bibliothèques globales installées. Les UUID sont déterministes.

Les références externes de modèles 3D sont retirées des empreintes placées pour éviter de lire un modèle non épinglé pendant un export. Les sources originales les conservent comme données inertes ; le manifeste documente cette omission. La géométrie 3D n'est pas validée par ce parcours.

Les fichiers portent le marqueur `PCBDEV_LIBRARY_DRAFT`. Les règles de carte restent des hypothèses de planification. La netlist produite directement depuis le modèle sert à la revue ; elle ne constitue pas une preuve indépendante que KiCad a interprété le schéma correctement. Cette preuve utilise un **export natif du schéma**, puis une comparaison modèle/schéma/PCB.

## Smoke natif reproductible

Avec KiCad 9 ou 10 réellement installé et `kicad-cli` accessible :

```sh
python -m pcb_dev.backends.smoke_libraries --output build/native-libraries
```

Le smoke échoue si le moteur réel est absent ou non pris en charge. Il conserve les commandes, hashes et rapports natifs. Ses conditions de réussite sont :

1. Les symboles et empreintes proviennent des sources locales épinglées ; le PCB garde les tailles, formes, couches, perçages et rotations attendus.
2. KiCad charge le schéma et exporte une netlist native contenant les trois composants, les nets attendus et les noms/types réels des broches.
3. La parité modèle/schéma/PCB passe pour le mapping nominal et échoue pour une modification intentionnelle d'une broche.
4. Les exports Gerber, perçages, positions et SVG de schéma chargent réellement les fichiers générés.
5. L'ERC natif du mapping nominal passe ; le DRC natif conserve des connexions non routées et **reste bloquant**.

Quand le module Python `pcbnew` est disponible, le smoke charge aussi la carte avec l'API native et compare les coordonnées absolues, orientations, tailles, perçages et nets de chacun des six pads. L'API de pads expose ces valeurs dans les versions [KiCad 9](https://docs.kicad.org/doxygen-python-9.0/classpcbnew_1_1PAD.html) et [KiCad 10](https://docs.kicad.org/doxygen-python-10.0/classpcbnew_1_1PAD.html). S'il est absent, le rapport l'indique comme indisponible ; les contrôles locaux bornés de géométrie et les chargements/exports CLI réels restent distincts, sans prétendre à une vérification native des coordonnées.

Le workflow [Native KiCad compatibility](../.github/workflows/native-kicad.yml) exécute ce smoke dans ses deux images officielles et conserve `build/native-libraries/smoke-report.json` avec les sources générées et rapports. Les tests unitaires de contrat vérifient les fixtures, les hashes, les refus et les comparateurs ; ils ne constituent pas une exécution de KiCad. La définition d'un workflow ne prouve pas sa réussite sur un commit : consulter ses résultats réels.

Une réussite du smoke signifie que ce fixture a été interprété comme attendu et que les blocages de routage ont été conservés. Elle n'autorise aucune fabrication.

## Provenance et licence des données KiCad

Les fichiers vendus dans [examples/library_divider/libraries](../examples/library_divider/libraries) proviennent de la release officielle **9.0.0** :

| Collection | Commit exact |
| --- | --- |
| Symboles KiCad | `a9012d8971ef2ea3243ffc9f19a5b4f153d8b334` |
| Empreintes KiCad | `e515f1c3213317d436e492c8c06620ef4caca84e` |

Seuls les symboles `R` et `Conn_01x02` ont été extraits de leurs collections ; leur contenu et les en-têtes ont été conservés. Les empreintes sont les octets originaux complets. [SOURCE-PROVENANCE.json](../examples/library_divider/SOURCE-PROVENANCE.json) contient les URL officielles à ces commits, les hashes des fichiers amont complets, les hashes des sous-ensembles distribués et les modifications de périmètre.

Ces données restent sous **CC-BY-SA 4.0 avec l'exception des bibliothèques KiCad**, avec attribution à la communauté KiCad. Elles ne deviennent pas MIT du fait de leur inclusion dans ce dépôt. L'[attribution](../examples/library_divider/ATTRIBUTION.md), le [document original de licence](../examples/library_divider/KICAD-LIBRARIES-LICENSE.md) et le [texte intégral CC-BY-SA](../examples/library_divider/CC-BY-SA-4.0.txt) accompagnent les fixtures et leurs snapshots de build. L'exception et les conditions de redistribution sont expliquées par la [source officielle KiCad](https://www.kicad.org/libraries/license/).
