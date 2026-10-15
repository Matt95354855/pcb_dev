# Moteurs CAO, routage et simulation

Le cœur de PCB Dev fonctionne avec Python 3.11 ou supérieur et sa bibliothèque standard. KiCad, Freerouting, Java et Ngspice sont des outils locaux facultatifs. Le programme ne télécharge et n'installe aucun moteur pendant un build.

## Génération KiCad : un aperçu éditable

```python
from pathlib import Path
from pcb_dev.backends import generate

files = generate(design.to_dict(), Path("build/kicad"))
```

`generate` reçoit un dictionnaire conforme au [contrat de données](CONTRACT.md), déjà validé. Il produit :

| Clé | Fichier | Contenu |
| --- | --- | --- |
| `schematic` | `.kicad_sch` | Symboles génériques autonomes, broches passives et labels globaux |
| `board` | `.kicad_pcb` | Contour rectangulaire, 2 ou 4 couches, pads et placements |
| `netlist` | `.net` | Netlist XML déterministe construite depuis le modèle |
| `project` | `.kicad_pro` | Projet et paramètres minimaux |
| `rules` | `.kicad_dru` | Règles illustratives de largeur et d'espacement |
| `manifest` | `generation-manifest.json` | Limites, références, empreintes demandées et empreintes SHA256 |

Les fichiers utilisent le format KiCad 9, lisible par les versions prises en charge 9 et 10. Les UUID sont déterministes. Les composants des blocs imbriqués conservent leurs références et leurs affectations explicites `numéro de broche → net global`. Les positions physiques fournies sont conservées ; les composants sans position reçoivent uniquement un placement d'aperçu.

**Ces fichiers ne sont pas qualifiés pour la fabrication.** Les géométries des empreintes sont génériques. Le nom d'une vraie empreinte fourni par le modèle est conservé dans `RequestedFootprint`, sans prétendre avoir chargé ou vérifié sa géométrie. Les broches des symboles sont toutes passives, ce qui limite la portée électrique de l'ERC. Aucun cuivre n'est routé. Le texte et les propriétés `PCBDEV_PREVIEW_ONLY`, ainsi que `fabrication_ready: false`, rendent cette limite explicite. Les règles illustratives ne constituent pas une validation de capacités ou de stackup JLCPCB.

La netlist générée depuis le modèle n'est pas une preuve indépendante de la connectivité du schéma. Utiliser l'export natif ci-dessous pour cette comparaison.

## Vérifications natives KiCad

```python
from pcb_dev.backends import KiCadBackend

backend = KiCadBackend()  # ou executable="/chemin/vers/kicad-cli"
diagnostic = backend.diagnose()
checks = backend.check(files["board"], files["schematic"], Path("build/native-checks"))
netlist = backend.export_netlist(files["schematic"], Path("build/native.net"))
```

Le diagnostic exige un `kicad-cli` fonctionnel de version majeure 9 ou 10. Les commandes de vérification utilisent `--format json --severity-all --exit-code-violations`. Le code natif `5` signifie que des violations ont été trouvées ; il est conservé comme preuve d'échec avec le rapport et ses messages. Un code `0` ne suffit pas : le rapport doit être récent, lisible, lié au bon fichier et ne contenir aucune violation. Les exclusions restent visibles et bloquent le résultat par défaut. Quand KiCad expose `ignored_checks`, ces contrôles désactivés bloquent également. Un champ `included_severities` présent doit annoncer toutes les sévérités ; les anciennes versions ne l'exposant pas restent compatibles.

Chaque preuve contient `stage`, `status` (`passed`, `failed` ou `unavailable`), `executed`, `input_sha256`, `tool_version`, `report_path`, `violations` et `returncode`. Une preuve valide ajoute `native_report` et `report_sha256`. Une absence du binaire, un timeout, un rapport absent ou mal formé, un schéma JSON inconnu ou une modification du fichier pendant le contrôle empêchent la réussite.

`export_netlist` exécute `sch export netlist --format kicadxml`, exige un document XML KiCad avec composants et nets, refuse les DTD/entités et conserve les SHA256 d'entrée et de sortie. Son résultat lie ainsi la netlist native au schéma contrôlé. La comparaison avec le modèle et le PCB reste une étape distincte. Cette méthode ne remplace pas le contrôle de cohérence hiérarchique d'un projet KiCad complet.

## Exports KiCad

```python
exports = backend.export(files["board"], files["schematic"], Path("build/exports"), layers=2)
```

`products` décrit séparément `gerbers`, `drill`, `positions`, `step` et `schematic_svg`. Chaque produit conserve son statut, ses fichiers réellement créés, ses empreintes SHA256, la commande et le résultat du moteur. Un répertoire temporaire neuf empêche de réutiliser un ancien résultat.

La validation vérifie les couches Gerber attendues et leurs en-têtes/fins, les enregistrements Excellon, les colonnes et coordonnées finies du CSV de placement, la structure du SVG et les marqueurs d'échange STEP. Elle ne certifie ni le routage, ni les empreintes, ni les capacités de fabrication. Une carte ne comportant aucun perçage peut légitimement ne pas générer de fichier Excellon ; le produit reste alors signalé sans artefact plutôt que déclaré réussi. L'export conserve toujours `fabrication_ready: false` ; une qualification doit se faire au niveau du processus de livraison.

## Freerouting : DSN vers SES

```python
from pcb_dev.backends import FreeroutingBackend

router = FreeroutingBackend(
    Path("tools/freerouting.jar"),
    expected_sha256="<SHA256 réel du JAR approuvé, 64 caractères hexadécimaux>",
    timeout=600,
)
session = router.route(Path("reviewed-board.dsn"), Path("build/routing"), max_passes=100, threads=1)
```

Le JAR est du code exécutable local : utiliser une version de confiance. `expected_sha256` est facultatif dans l'API, mais recommandé pour figer l'outil revu. L'adaptateur utilise l'interface headless de Freerouting 2.2 ou supérieur, désactive l'API et les statistiques, limite les passes/threads et produit un fichier SES dans un espace temporaire isolé. Le hash du JAR et celui du DSN figurent dans le résultat ; la version du manifeste JAR est conservée quand elle est présente. Aucun fichier de règles adjacent au DSN source n'est repris implicitement.

`status: passed` signifie qu'une session structurée a été produite par le moteur. Cela ne prouve pas que tous les réseaux sont routés ou que les espacements sont corrects. `routing_verified`, `board_updated` et `fabrication_ready` restent faux. Il faut importer la session dans le PCB réel, contrôler la correspondance des nets et relancer le DRC natif. L'adaptateur n'invente pas d'import SES dans `kicad-cli`, qui n'expose pas cette opération dans l'interface utilisée ici.

## Ngspice : simulation locale autonome

```python
from pcb_dev.backends import NgSpiceBackend

simulation = NgSpiceBackend(timeout=120).simulate(
    Path("reviewed-circuit.cir"), Path("build/simulation")
)
```

La netlist SPICE est explicite et indépendante des symboles génériques de l'aperçu. Les analyses `.op`, `.tran`, `.ac` et `.dc` sont prises en charge, ainsi qu'un sous-ensemble de cartes de composants, modèles et mesures. Le moteur doit produire un fichier de données non vide et un journal sans échec d'analyse. Les mesures nommées demandées doivent toutes avoir une valeur finie ; le résultat ne les compare pas automatiquement à des limites d'acceptation métier.

Les scripts `.control`, commandes shell, inclusions de modèles externes, sources lisant des fichiers et modèles chargés dynamiquement sont refusés avant toute exécution. Les modèles utilisés doivent donc être présents dans la netlist autonome. `-n` désactive le chargement des fichiers `.spiceinit` utilisateur. Cela ne constitue pas un bac à sable du binaire système lui-même : comme KiCad et Java, l'exécutable installé doit être de confiance.

Ngspice ne peut pas évaluer `.meas` dans un batch qui écrit simultanément avec `-r`. L'adaptateur effectue donc un premier passage de données avec les cartes de mesure retirées, puis, si nécessaire, un second passage sans `-r` pour les mesures, avec des projections `.print` générées. Il conserve les deux entrées effectivement exécutées, les journaux, le fichier de données, les commandes et leurs hashes. Les passages sont bornés séparément par le timeout. `hardware_validated` reste faux : la fidélité des modèles et les critères physiques nécessitent une revue d'ingénierie.

## Exécution et limites de sécurité

`ToolRunner` n'accepte que `kicad-cli`, `ngspice` et `java`, y compris leurs noms Windows et chemins explicites. Il transmet un tableau d'arguments à un processus sans shell, ferme l'entrée standard et exige un répertoire de travail explicite. Les sorties conservées sont limitées à 64 Kio par flux par défaut. Un timeout interrompt le processus ; sous POSIX, le groupe de processus est également arrêté pour fermer les descendants et leurs pipes. Les outils ne sont jamais installés automatiquement.

## Validation réelle et tests unitaires

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_backends.py -v
PYTHONPATH=src python3 -m pcb_dev.backends.smoke --output build/native-smoke
PYTHONPATH=src python3 -m pcb_dev.backends.smoke_simulation --output build/native-ngspice
```

Les tests unitaires utilisent des réponses de moteurs simulées pour couvrir les états absents, erreurs, timeouts, violations, exclusions et artefacts incorrects. Ils ne constituent pas une exécution de KiCad, Freerouting ou Ngspice.

Le workflow [Native KiCad compatibility](../.github/workflows/native-kicad.yml) utilise les images officielles `kicad/kicad:9.0.9` et `kicad/kicad:10.0.2`, figées à des versions de publication. Il exige que les fichiers générés soient réellement chargés par KiCad, vérifie les références et les nets globaux de la netlist native, exporte les aperçus et conserve les violations attendues d'une carte non routée. Un job séparé vérifie réellement un circuit RC avec Ngspice. Les rapports et journaux sont publiés comme artefacts CI. La présence de ces workflows dans le dépôt ne prouve pas leur réussite : consulter leur exécution sur le commit concerné.

## Références des interfaces

- [Interface CLI KiCad 10](https://docs.kicad.org/10.0/en/cli/cli.html) et [KiCad 9](https://docs.kicad.org/9.0/en/cli/cli.html).
- [Formats natifs KiCad](https://dev-docs.kicad.org/en/file-formats/), [schéma JSON DRC](https://schemas.kicad.org/drc.v1.json) et [schéma JSON ERC officiel](https://gitlab.com/kicad/code/kicad/-/raw/master/resources/schemas/erc.v1.json).
- [Image Docker officielle KiCad et politique des tags](https://hub.docker.com/r/kicad/kicad), [sources de l'image](https://gitlab.com/kicad/packaging/kicad-docker).
- [Arguments officiels Freerouting](https://github.com/freerouting/freerouting/blob/master/docs/command_line_arguments.md).
- [Manuel Ngspice](https://ngspice.sourceforge.io/docs/ngspice-manual.pdf), sections « Batch mode » et « Measurements after AC, DC and Transient Analysis ».
