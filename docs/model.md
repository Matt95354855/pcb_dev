# Modèle de design v1

Les fichiers sont des objets JSON UTF-8. `load_design` refuse les clés dupliquées,
champs inconnus, nombres non finis, booléens à la place de nombres, références
dupliquées et extrémités de connexion inconnues. Limite : 16 Mio et profondeur
de blocs bornée. Le parseur Python est l'autorité sur les contraintes sémantiques.

## Racine

```json
{
  "schema_version": 1,
  "name": "Ma carte",
  "version": "0.1.0",
  "board": {"width_mm": 40, "height_mm": 30, "layers": 2, "manufacturer": "jlcpcb"},
  "root": {"id": "board", "children": []},
  "connections": [],
  "metadata": {}
}
```

Les dimensions sont strictement positives, les couches `2` ou `4`.
La cible de design v1 est `jlcpcb`. Les profils `pcbway` et `oshpark` restent des
profils de planification consultables ; ils ne sont pas des cibles qualifiées
du parseur v1.

## Blocs et paramètres

| Champ | Description |
| --- | --- |
| `id` | Identifiant ASCII sans point ni espace ; compose le chemin absolu |
| `version`, `description` | Version et description lisible |
| `ports`, `children`, `components` | Membres du bloc ; leurs identifiants ne peuvent pas se chevaucher |
| `parameters` | Valeurs JSON locales, héritées par les enfants avec surcharge locale |
| `equations` | Sorties calculées à partir de paramètres locaux/hérités et d'autres équations |
| `constraints` | Objets `expression` et `message` ; une expression fausse bloque le modèle |
| `depends_on` | Chemins absolus de blocs requis ; cycles et références inconnues refusés |
| `frozen`, `frozen_hash` | Gel contextuel, invalidé par une modification pertinente |
| `resources` | Déclarations de ressources vérifiées selon les conventions ci-dessous |
| `variants` | Données de variantes d'assemblage, voir les paquets de blocs |

Les équations acceptent les opérateurs arithmétiques, les fonctions bornées
`abs`, `min`, `max`, `round`, `sqrt`, et les constantes `pi` et `e`.
Les contraintes acceptent comparaisons et combinaisons booléennes. Les accès
d'attributs, imports, compréhensions et appels arbitraires sont interdits.
Des limites d'expression, d'imbrication et de calcul empêchent les valeurs
pathologiques. Une valeur booléenne n'est jamais un nombre.

La portée d'une équation est locale et héritée : `depends_on` exprime l'ordre
et l'impact, pas un import implicite de toutes les variables d'un autre bloc.
Une sortie d'équation ne remplace pas automatiquement un champ de port ou une
valeur de composant. Les paramètres physiques restent explicites dans cette version.

## Ports et connexions

```json
{
  "id": "vout",
  "kind": "power",
  "direction": "output",
  "voltage_min": 3.25,
  "voltage_max": 3.35,
  "max_current_ma": 300,
  "current_draw_ma": 0,
  "required": true,
  "polarity": "positive"
}
```

Types : `power`, `ground`, `digital`, `analog`, `differential`, `bus`.
Directions : `input`, `output`, `bidirectional`, `passive`.
Autres données : `logic_standard`, `protocol`, `impedance_ohm`.
Les enveloppes source doivent être entièrement contenues dans celles des
entrées. Les données critiques de puissance manquantes bloquent le contrôle.
Les charges d'un même net sont additionnées une fois par port. Des sources
multiples ne sont pas supposées partageables en parallèle.

```json
{"source": "board.power.vout", "target": "board.mcu.vcc", "net": "3V3"}
```

Les connexions sont au niveau racine. Des connexions portant le même nom de
net sont électriquement réunies même si leurs segments sont séparés. Les
ports obligatoires non connectés et les sorties concurrentes sont signalés.

## Composants

```json
{
  "id": "bypass",
  "reference": "C1",
  "value": "100n",
  "footprint": "Capacitor_SMD:C_0603_1608Metric",
  "pins": {"1": "3V3", "2": "GND"},
  "parameters": {"voltage_rating_v": 10, "applied_voltage_v": 3.35, "derating_factor": 0.8},
  "mpn": "",
  "lcsc": "",
  "fitted": true,
  "position": {"x_mm": 10, "y_mm": 10, "rotation": 0, "side": "top"}
}
```

Les numéros de pins pointent vers des noms de nets globaux. Les connexions de
ports et les pins physiques restent explicites. Un port peut déclarer `net` et
`pin_bindings` pour vérifier leur cohérence exacte, avec un mode strict exigeant
sa réalisation montée. Voir le [contrat ports/broches](physical-ports.md).
La parité compare les pins déclarées aux sources CAO, sans certifier un pinout de datasheet.

`fitted: false` exclut le composant des BOM/CPL. Une position absente est
autorisée dans le modèle, puis omise du CPL indicatif ; le mode strict de
l'API de placement la refuse. Les positions sont en millimètres, les rotations
en degrés. Une position dans le contour ne constitue pas un contrôle de collision.

Paires de paramètres vérifiées lorsqu'elles sont déclarées :
`voltage_rating_v`/`applied_voltage_v`, `current_rating_ma`/`applied_current_ma`,
`power_rating_w`/`power_dissipation_w`. `derating_factor` applique une marge
déclarée au rating. Aucune valeur n'est extraite automatiquement d'une datasheet.

## Ressources

```json
{
  "i2c": [{"bus": "I2C0", "address": "0x48"}],
  "power_sequence": {"after": ["board.power"], "delay_ms": 5, "required_delay_ms": 2}
}
```

Les adresses I²C entières ou hexadécimales sont vérifiées sur le même bus,
y compris lorsque le bus est identifié par des ports reliés. Les collisions,
adresses réservées ou invalides sont signalées. `power_sequence` contient
des prédécesseurs absolus dans `after` (ou directement une liste sans délais) ;
les cycles et délais invalides sont signalés.
Il s'agit d'une vérification des déclarations, pas d'une simulation de rampe.

## API

```python
from pcb_dev.model import load_design
from pcb_dev.checks import check_design
from pcb_dev.expressions import resolve_parameters
from pcb_dev.graph import dependency_order, dependents

design = load_design("examples/usb_mcu_led.json")
assert not check_design(design).blocking
values = resolve_parameters(design)
order = dependency_order(design)
affected = dependents(design, "board.power")
```

Les rapports comprennent le hash du design. La complétude et l'origine native
sont distinctes d'un simple statut sans erreur. Un rapport Python ne remplace
jamais un rapport KiCad natif.
