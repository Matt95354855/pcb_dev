# Blocs, équations, transactions et paquets

Le modèle décrit les intentions électriques et le placement déclaré. Il reste
lisible dans Git. Les vérifications Python portent sur ces données ; elles ne
remplacent ni les bibliothèques KiCad réelles, ni ERC/DRC, ni une revue technique.

## Arbre de blocs et paramètres

Un chemin inclut toujours l'identifiant racine : `board.power` désigne un bloc,
`board.power.vout` un port. Les identifiants utilisent uniquement les lettres
ASCII, chiffres, `_` et `-`. Chaque enfant, composant et port possède un
identifiant distinct dans son bloc. Les références de composants sont uniques
dans toute la carte, sans distinction de casse.

```json
{
  "id": "power",
  "parameters": {"vin_v": 5, "r1_ohm": 1000, "r2_ohm": 2000},
  "equations": {"vout_v": "vin_v * r2_ohm / (r1_ohm + r2_ohm)"},
  "constraints": [
    {"expression": "3.0 <= vout_v <= 3.6", "message": "Sortie hors plage"}
  ]
}
```

Les enfants héritent des paramètres **résolus** du parent. Leurs paramètres
locaux remplacent les valeurs héritées. Leurs équations remplacent ensuite les
valeurs locales, dans un ordre déterminé par leurs références : une équation
peut utiliser une autre équation déclarée plus loin dans le fichier. Les cycles
et noms inconnus produisent une erreur qui précise le bloc et l'équation.
Surcharger un paramètre d'entrée dans un enfant ne recalcule pas les équations
déjà calculées par son parent ; redéclarer l'équation dans cet enfant si ce
comportement est souhaité.

Les noms utilisables dans une équation sont des identifiants Python ordinaires,
comme `current_ma`. Les valeurs non numériques peuvent servir de métadonnées
dans les paramètres ; les équations refusent de les traiter comme des nombres.
Les booléens sont également refusés comme nombres.

Opérations prises en charge : `+`, `-`, `*`, `/`, `//`, `%`, `**`, parenthèses,
`abs`, `min`, `max`, `round`, `sqrt`, et constantes `pi` et `e`. Les contraintes
ajoutent `<`, `<=`, `>`, `>=`, `==`, `!=`, les comparaisons chaînées, `and`, `or`
et `not`. Une contrainte doit rendre un booléen ; la conversion implicite d'un
nombre en booléen est interdite. Une équation ne peut pas utiliser de chemin de
bloc, d'attribut, d'indexation, de fichier, de réseau ou d'appel Python arbitraire.
Il n'y a aucun `eval`/`exec`. La longueur, la profondeur, le nombre de nœuds,
les exposants et les résultats sont bornés. Les unités restent explicites dans
les noms et dans le contrat ; aucun solveur dimensionnel n'est présent.

`depends_on` contient des chemins **absolus de blocs**. Il contrôle l'ordre de
construction et les requêtes d'impact ; il n'importe pas les paramètres du bloc
cible. L'héritage suit l'arbre, indépendamment de ces dépendances explicites.

```python
from pcb_dev.model import load_design
from pcb_dev.expressions import resolve_parameters
from pcb_dev.graph import dependency_order, dependents

design = load_design("examples/usb_mcu_led.json")
print(dependency_order(design))
print(resolve_parameters(design))
print(dependents(design, "board.power"))
```

## Gel d'un bloc

Un gel mémorise le SHA-256 du sous-arbre, de ses paramètres effectifs, de ses
dépendances transitives, des autres branches de ses nets globaux et du contexte
de carte. Il détecte aussi un changement d'une dépendance d'un descendant ou
l'ajout d'une charge sur un rail partagé. Un net de masse partagé peut donc
étendre largement ce contexte ; un bloc électriquement isolé reste indépendant. Les
champs `frozen` et `frozen_hash` sont exclus de ce calcul pour éviter toute
autoréférence. L'empreinte complète du design les inclut pour la provenance.

```python
from pcb_dev.model import freeze_fingerprint

path = "board.power"
block = dict(design.walk_blocks())[path]
# À enregistrer après la validation technique du bloc et de son contexte.
block.frozen = True
block.frozen_hash = freeze_fingerprint(design, path)
```

Un gel est une protection de changement, pas une certification. Les transactions
refusent de toucher un bloc gelé, ses descendants, ou un contexte amont qui
invaliderait son empreinte. Un gel absent ou déjà périmé bloque également les
transactions. Le mécanisme de proposition IA ne dispose d'aucune opération de
dégel ni de modification des empreintes.

## Propositions atomiques et annulation

`apply_patch(design, operations)` retourne une copie strictement validée. Il ne
modifie jamais l'entrée, même si la dernière opération échoue. Les opérations
disponibles possèdent des champs exacts ; aucun chemin JSON arbitraire ni chemin
de fichier n'est accepté.

| Opération | Champs supplémentaires |
| --- | --- |
| `set_parameter` | `block`, `name`, `value` |
| `add_block` | `parent`, `block` (objet JSON de bloc) |
| `update_block` | `block`, `changes` |
| `connect` | `source`, `target`, `net` |
| `remove_connection` | `source`, `target`, `net` |

`update_block` peut remplacer `version`, `description`, `parameters`,
`equations`, `constraints`, `resources` et `variants`. Il ne peut remplacer ni
identifiants, ni ports, ni gel, ni porte de validation. Une proposition peut
ajouter un bloc puis connecter ses ports dans le même lot : les vérifications
portent sur l'état final du lot, sans état partiel appliqué.

Par défaut, les nouveaux blocages de préflight sont refusés. Un blocage existant
doit rester identique, sauf amélioration monotone reconnue d'un bilan de courant,
d'une limite de composant ou d'un délai de séquencement. Une surcharge déjà
présente ne peut donc pas être aggravée. La résolution d'un blocage est autorisée.
L'option Python explicite `prevent_regressions=False` permet de construire un
brouillon incomplet ; elle ne désactive ni le schéma strict, ni les protections
de gel, ni la porte de fabrication. Elle n'est pas exposée aux outils du modèle IA.

```python
from pcb_dev.transactions import DesignSession

design = load_design("examples/usb_mcu_led.json")
session = DesignSession(design, actor="ingénieur")
session.apply([
    {"op": "set_parameter", "block": "board", "name": "budget_ma", "value": 500}
])
session.save("design-session.json")
restored = DesignSession.load("design-session.json")
restored.undo()  # Annule le lot entier en une opération.
```

Une session mémorise l'état initial, l'état courant et les événements attribués,
horodatés et chaînés par SHA-256. Son chargement vérifie la chaîne et rejoue les
opérations et annulations pour comparer l'état final. La sauvegarde utilise un
fichier temporaire synchronisé puis un remplacement atomique. Conserver un hash
de confiance hors de la session pour détecter un remplacement intégral de son
historique ; une chaîne de hashes locale n'est pas une signature d'identité.

## Paquets JSON épinglés

Un paquet contient `schema_version`, `name`, une version sémantique exacte,
`root` et `connections`. L'extraction d'un sous-arbre capture ses paramètres
effectifs à la frontière et ses connexions internes. Les connexions externes
restent des interfaces à reconnecter par l'appelant. Les dépendances et
séquencements qui sortent du sous-arbre sont refusés : ils ne sont jamais
réinterprétés silencieusement. Les gels sont retirés lors de l'extraction et
refusés à l'import : un paquet importé ne peut pas se déclarer validé par avance.

```python
from pcb_dev.packages import create_package, write_package, lock_package
from pathlib import Path

# La carte complète est autonome ; son bloc power dépend de board.usb et
# ne peut pas être extrait seul sans redéfinir cette frontière.
package = create_package(design, "board", name="usb-mcu-led-demo", version="0.1.0")
Path("blocks").mkdir(exist_ok=True)
write_package(package, "blocks/usb-mcu-led-demo.json")
lock_package("blocks/usb-mcu-led-demo.json", "blocks.lock.json")
```

Créer les dossiers parents avant l'écriture. Le lock contient un fichier relatif,
une version exacte et le SHA-256 des octets du paquet. Les chemins absolus,
traversées `..`, chemins Windows et liens symboliques qui sortent du répertoire
du lock sont refusés. Le contenu est uniquement constitué de JSON borné : aucun
script, hook, module Python, archive ou téléchargement n'est exécuté. Ce contrôle
protège l'intégrité du contenu épinglé, pas sa justesse électronique.

```python
from pcb_dev.packages import load_locked_package, instantiate_package

package = load_locked_package("blocks.lock.json", "usb-mcu-led-demo")
design = instantiate_package(
    design, "board", package,
    instance_id="demo_aux", reference_prefix="AUX_", net_prefix="AUX_",
    shared_nets=["GND"],
)
```

Chaque instance requiert un préfixe explicite pour ses références et ses nets.
`AUX_` transforme `R1` en `AUX_R1`, `VCC` en `AUX_VCC` et conserve seulement les
nets explicitement déclarés partagés, ici `GND`. Les chemins des dépendances,
connexions, séquencements et réservations I2C reconnus sont remappés. Les chaînes
arbitraires dans d'autres métadonnées ne sont pas substituées. Les doublons de
références et chemins sont encore rejetés par le modèle final.

`instantiate_package` construit un brouillon structurel en protégeant les gels.
Reconnecter ses ports de frontière puis lancer le préflight. Le paquet ne
prouve ni correspondance symbole/empreinte, ni capacité électrique, ni stock
fournisseur, ni conformité de fabrication.

## Variantes d'assemblage

Les variantes appliquent des choix explicites de montage. Les chemins des
composants sont relatifs au bloc qui porte la variante, sans son propre id.

```json
{
  "variants": {
    "debug": {
      "dnp": ["sensor.decoupling_extra"],
      "fit": ["debug_led"],
      "parameters": {"sample_rate_hz": 100}
    }
  }
}
```

Passer `variant="debug"` lors de l'instanciation. `dnp` met `fitted=false` et
`fit` met `fitted=true`, sans supprimer les composants ni leur connexions.
Un chemin inconnu, dupliqué ou présent dans les deux listes est refusé. Les
paramètres de variante remplacent ceux du paquet ; les paramètres explicites de
l'instance s'appliquent ensuite. Les équations se recalculent lors de la
résolution. Une variante n'est jamais appliquée à un bloc gelé.
