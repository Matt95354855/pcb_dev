# Relier les ports aux broches physiques

Un port décrit une interface de bloc ; une broche décrit un contact sur un net
KiCad global. Depuis la version 0.3, leur relation peut être explicite :

```json
{
  "id": "supply", "kind": "power", "direction": "input",
  "net": "VIN",
  "pin_bindings": [{"component": "r1", "pin": "1"}],
  "voltage_min": 4.75, "voltage_max": 5.25, "current_draw_ma": 0.25
}
```

Le composant `r1` doit appartenir au **même bloc** et déclarer
`"pins": {"1": "VIN", ...}`. Les identifiants de cibles restent locaux lors
de l'instanciation d'un paquet ; ses nets sont préfixés sauf `shared_nets` explicites.

## Contrat

- `net` est une intention globale exacte, sensible à la casse. Des cibles non
  vides exigent cette intention. Chaque cible contient exactement deux chaînes :
  `component` et `pin`. Le composant et sa broche doivent exister.
- Les cibles vers un autre bloc sont refusées. Une broche ne peut être répétée
  ni réaliser deux ports du même bloc. Plusieurs cibles d'un port restent sur
  un seul net ; une liste ne représente pas un bus SDA/SCL ou une paire D+/D−.
- Nets des cibles, intentions des ports reliés et noms de leurs connexions
  doivent être identiques. Aucun alias logique ne fusionne implicitement deux
  nets physiques. Toute contradiction est bloquante, même en brouillon.
- Des ports portant la même intention de net participent aux conflits/budgets
  même sans arête explicite. La règle existante `required` continue d'exiger
  une connexion logique déclarée.
- Chaque cible doit être montée pour prouver la réalisation. Une cible DNP
  laisse la couverture incomplète. Un NC explicite n'a pas de net dans
  `Component.pins` et ne peut servir de cible.

Les tensions, courants et directions restent déclarés. Ce contrôle n'infère
pas un rôle de datasheet depuis le nom d'une pin, ne suit pas un signal à
travers un composant et ne certifie pas le cuivre ou une empreinte.

## Brouillon et mode strict

Les ports abstraits restent autorisés par défaut, avec avertissement et
couverture non prouvée. Les champs absents `net`/`pin_bindings` sont omis de
la sérialisation canonique : les hashes des anciens designs sont préservés.

`--require-physical-ports` bloque chaque port non réalisé, même si `required`
est faux. Ce champ concerne une connexion logique, pas une permission de
supprimer la réalisation physique d'une interface.

```sh
pcb-dev check examples/library_divider/port_design.json --require-physical-ports
pcb-dev build examples/library_divider/port_design.json \
  --bindings examples/library_divider/port_bindings.json \
  --require-physical-ports --output build/physical-divider
pcb-dev verify build/physical-divider
```

L'exemple expose quatre interfaces alimentation/masse liées à J1, R1 ou R2.
J1 représente une source externe idéale **déclarée** de 5 V : aucune alimentation
réelle ou référence fabricant n'est qualifiée. Les pins internes du diviseur
ne doivent pas toutes devenir des ports publics.

`metadata.physical_ports` contient les nombres de ports réalisés, leurs cibles,
leur état monté et le statut de couverture. Zéro port donne `not_applicable`,
sans preuve globale de complétude. La visionneuse et `design.md` présentent
cette relation ; le lock enregistre la politique stricte.

## Vérification native

```sh
pcb-dev native-check examples/library_divider/port_design.json \
  --bindings examples/library_divider/port_bindings.json \
  --require-physical-ports \
  --board build/physical-divider/kicad/Physical_Port_Divider.kicad_pcb \
  --schematic build/physical-divider/kicad/Physical_Port_Divider.kicad_sch \
  --output build/physical-native
```

Le statut reste bloqué tant que la carte n'est pas routée. La commande réunit
le préflight, ERC/DRC réels et la parité des bibliothèques. Elle vérifie les
hashes des entrées entre les étapes et n'accorde aucune approbation de fabrication.
Voir la [parité NC explicite](bound-parity.md) et la
[génération depuis les bibliothèques](library-generation.md).

Le gel inclut les intentions de nets et leur contexte. L'agent peut proposer
un nouveau bloc lié, mais ne peut réécrire les intentions physiques existantes.
Les liaisons hiérarchiques vers des descendants, le pinmux et la qualification
datasheet restent à compléter.
