# Du modèle de carte à une netlist SPICE explicite

PCB Dev peut produire une netlist déterministe pour des composants dont le comportement a été déclaré explicitement. Il ne déduit aucun modèle d'un nom, d'une référence, d'une empreinte ou de la valeur d'affichage. Un MCU, un régulateur, une diode ou un autre composant actif sans modèle pris en charge provoque un refus, sans substitution inventée.

## Utilisation Python

```python
from pathlib import Path
from pcb_dev.model import load_design
from pcb_dev.backends import generate_spice, NgSpiceBackend

design = load_design("examples/rc_filter.json")
generated = generate_spice(
    design,
    {"kind": "tran", "step_s": 10e-6, "stop_s": 10e-3, "uic": True},
    Path("build/rc.cir"),
    ground_nets=["GND"],
)
simulation = NgSpiceBackend().simulate(
    Path(generated["path"]), Path("build/rc-results")
)
```

`generate_spice` accepte un objet `Design` ou son dictionnaire JSON. Il revalide le modèle, vérifie tous les composants montés de la sélection et génère le fichier demandé avec un manifeste `<nom>.cir.json`. Les deux destinations doivent être nouvelles. Le résultat `status: generated` indique uniquement une génération : `executed` et `hardware_validated` restent faux. L'exécution facultative est distincte et documentée dans [les moteurs externes](backends.md).

L'API à appeler depuis une interface CLI est :

```python
generate_spice(design, analysis, output, block_path=None, *, ground_nets=("GND", "0"))
```

Elle lève `SpiceError` avant toute écriture lorsqu'un modèle ou une analyse est incomplet, ambigu ou non pris en charge. L'interface doit transmettre les erreurs à l'utilisateur et ne pas annoncer une simulation réussie après une simple génération.

Les fichiers [op.json](../examples/op.json) et [tran.json](../examples/tran.json) fournissent des analyses prêtes à transmettre à l'option CLI `--analysis`. Le second est destiné au filtre RC et demande explicitement le démarrage avec les conditions initiales.

## Modèles autorisés

Chaque composant monté doit porter `parameters.spice` :

```json
{
  "kind": "R",
  "value": "10k",
  "pins": ["1", "2"],
  "tolerance": 0.01
}
```

`kind` vaut exactement `R`, `C`, `L`, `V` ou `I`. Le modèle ne prend actuellement en charge que deux bornes. Les deux numéros physiques doivent être distincts, exister dans `component.pins` et couvrir toutes ses broches ; aucune broche supplémentaire n'est ignorée. L'ordre détermine la première et la seconde borne SPICE. Une alternative explicite est `"pins": {"positive": "1", "negative": "2"}`.

| Modèle | Valeur | Paramètres facultatifs |
| --- | --- | --- |
| `R` | Résistance strictement positive, en ohms | `tolerance` |
| `C` | Capacité strictement positive, en farads | `tolerance`, `initial_voltage_v` |
| `L` | Inductance strictement positive, en henrys | `tolerance`, `initial_current_a` |
| `V` | Tension DC finie, signée, en volts | `ac_magnitude`, `ac_phase_deg` |
| `I` | Courant DC fini, signé, en ampères | `ac_magnitude`, `ac_phase_deg` |

Pour `V`, la tension est celle de la première borne par rapport à la seconde. Pour `I`, le courant positif circule de la première borne vers la seconde. Une source DC n'impose pas automatiquement de stimulus AC : celui-ci doit être déclaré par `ac_magnitude`. La phase, en degrés, exige une amplitude explicite. Les conditions initiales ne sont valides que sur le type de composant correspondant et leur effet dépend de l'analyse, notamment de l'option `uic` en transitoire.

`value` accepte un nombre JSON fini ou un littéral strict, par exemple `10000`, `"4.7k"`, `"1u"` ou `"1e-6"`. Les suffixes autorisés sont `f`, `p`, `n`, `u`, `m`, `k`/`K`, `M`/`Meg`/`meg`/`MEG`, `G` et `T`. Il n'y a ni unité finale, ni expression, ni espace, ni carte SPICE brute. Ici `m` signifie milli et `M` méga ; le générateur les convertit en nombres non ambigus avant de les transmettre à Ngspice, dont la convention native pour `M` diffère. Les débordements, sous-débordements non nuls et valeurs non finies sont refusés.

La tolérance est une fraction numérique finie entre 0 et 1, valable uniquement pour R/C/L. Elle figure dans le manifeste. **La simulation reste nominale : aucune distribution, analyse de coins ou simulation Monte Carlo n'est effectuée.** Les valeurs d'affichage et les paramètres de derating du modèle de carte ne remplacent pas le modèle SPICE explicite.

## Analyses

Les champs numériques d'analyse sont des nombres JSON finis en unités SI, sans suffixe textuel. Les champs inconnus sont refusés.

| Analyse | Objet minimal | Sens |
| --- | --- | --- |
| Point de fonctionnement | `{"kind":"op"}` | Calcul DC nominal |
| Transitoire | `{"kind":"tran","step_s":0.00001,"stop_s":0.01}` | Pas de sortie et temps final, en secondes |
| Petit signal AC | `{"kind":"ac","scale":"dec","points":20,"start_hz":1,"stop_hz":100000}` | Échelle `dec`, `oct` ou `lin` et plage en hertz |
| Balayage DC | `{"kind":"dc","source":"J1","start":0,"stop":5,"step":0.5}` | Référence d'origine d'une source V/I incluse et valeurs de balayage |

En transitoire, `start_s` vaut 0 par défaut, `uic` vaut faux et `max_step_s` est facultatif. `0 ≤ start_s < stop_s`, le pas doit être positif et ne pas dépasser la durée. `uic: true` utilise les conditions initiales sans calcul préalable du point de fonctionnement, selon le comportement natif de Ngspice.

L'AC exige au moins une source explicitement dotée d'une amplitude AC positive. `points` est un entier strict : nombre total pour `lin`, nombre de points par décade ou octave dans les autres cas. Le balayage DC utilise la référence déclarée dans la carte, puis la traduit vers sa référence SPICE stable ; le pas doit avoir le bon signe. Les demandes dépassant un million de points/intervalles nominaux sont refusées.

Toutes les analyses acceptent les tolérances numériques facultatives `reltol` (strictement positive et au plus 1), `abstol_a` et `vntol_v` (strictement positives). Seules les options fournies sont émises. Ce sont des tolérances de calcul, distinctes des tolérances physiques de composants.

## Nets, masses et sélection de bloc

Les masses sont des noms exacts et sensibles à la casse. Les conventions par défaut sont `GND` et `0` ; `ground_nets=["AGND"]` peut les remplacer explicitement. Au moins un net de la sélection doit correspondre à cette configuration. Les alias déclarés sont fusionnés sur le nœud SPICE `0`, sans deviner la masse à partir d'un nom approchant.

Tous les autres noms de nets et toutes les références sont transformés en identifiants sûrs et stables. Le manifeste conserve `net_map` et `reference_map`. Les chaînes originales ne sont jamais insérées comme cartes exécutables, même si elles contiennent des espaces, caractères spéciaux ou sauts de ligne. L'ordre des modèles et le contenu généré sont déterministes.

`block_path="board.divider"` sélectionne ce bloc et ses descendants. Les composants marqués `fitted: false` sont exclus et tracés. Tout composant monté de la sélection doit être modélisable ; les composants extérieurs sont omis. **Aucun stimulus, charge, terminaison ou autre condition à la frontière d'un bloc n'est ajouté automatiquement.** Une analyse demandant une source absente échoue. Le manifeste rappelle cette limite.

## Exemples fournis et portée

- [Filtre RC](../examples/rc_filter.json) : résistance idéale de 1 kohm, condensateur idéal de 1 uF et source de laboratoire idéale de 5 V déclarée au connecteur. Les analyses OP, transitoire et AC sont décrites dans `metadata.simulation.analyses`. Pour le transitoire, le condensateur part explicitement de 0 V avec `uic`.
- [Diviseur de tension](../examples/voltage_divider.json) : deux résistances idéales de 10 kohms et une source idéale de 5 V. La valeur nominale sans charge de la sortie est de 2,5 V ; le connecteur est ici la frontière d'une alimentation de laboratoire déclarée explicitement.

Les ports typés de ces petites fixtures sont vides : leurs réseaux physiques sont explicites dans les broches, sans inventer de contrat d'interface. Le preflight du modèle peut donc réussir sans constituer un ERC natif, une qualification de bibliothèque ou une validation de fabrication. Les sources idéales sont des hypothèses de banc d'essai ; elles ne constituent pas des modèles physiques de connecteurs ou d'alimentations réelles.

Les empreintes restent des identités demandées pour l'aperçu. Le routage, les parasites, les modèles non linéaires, la fidélité des composants réels et les critères de fabrication doivent être traités séparément.

## Vérification

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_spice_generation.py -v
PYTHONPATH=src python3 -m pcb_dev.backends.smoke_simulation --output build/native-ngspice
```

Les tests de génération ne nécessitent aucun moteur. Le smoke natif et le job CI Ngspice exécutent réellement le moteur quand il est installé ; leurs rapports, entrées, données et journaux sont conservés. Consulter le résultat CI sur le commit concerné avant d'affirmer que les exemples ont été simulés avec succès.
