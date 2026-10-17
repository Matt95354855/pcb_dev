# Découplage déclaré et proximité des vrais pads

Le découplage comporte deux preuves distinctes : une vérification du besoin et du
câblage déclarés avant layout (C6), puis une mesure sur les pads du PCB (G11).
Aucune valeur de condensateur, tension de service, limite de distance ou exigence
de datasheet n'est déduite du texte du composant ou inventée par le contrôleur.

## Déclarer l'exigence

Chaque bloc peut porter `resources.decoupling`, une liste de règles. Les chemins
de composants sont relatifs au bloc propriétaire ; ils peuvent viser ses descendants.
Voici la règle du [projet de test](../examples/decoupling/design.json), appartenant
au bloc `board.load` :

```json
{
  "id": "supply_pin_1",
  "power_pin": {"component": "j2", "pin": "2"},
  "capacitor": {"component": "decaps.c1", "power_pin": "1", "return_pin": "2"},
  "power_net": "VDD",
  "return_net": "GND",
  "capacitance_min_f": 9e-8,
  "capacitance_max_f": 1.1e-7,
  "rail_max_voltage_v": 3.6,
  "voltage_derating_factor": 0.5,
  "max_distance_mm": 1.2,
  "source": {
    "kind": "user_requirement",
    "reference": "pcb-dev disposable decoupling test fixture",
    "revision": "1",
    "section": "Explicit synthetic acceptance conditions, not a datasheet"
  }
}
```

Le condensateur déclare séparément `parameters.capacitance_f` et
`parameters.voltage_rating_v`, en unités SI numériques. Le contrôleur vérifie
les références et broches, les composants montés, les nets exacts distincts,
les bornes de capacité et `rail_max_voltage_v <= voltage_rating_v * voltage_derating_factor`.
Un condensateur DNP ne satisfait pas une règle. Les nets des règles sont remappés
avec ceux des composants lors de l'instanciation d'un paquet ; les nets partagés
conservent leur nom explicitement demandé.

La source exige un type (`user_requirement` ou `datasheet`), une référence, une
révision et une section. Ce sont des déclarations à relire : `source_verified`
reste faux. Le logiciel ne télécharge ni n'authentifie une datasheet derrière ces
champs. L'absence de règles ne signifie pas que toutes les broches d'alimentation
ont leur condensateur ; la couverture reste limitée aux règles déclarées.

```sh
pcb-dev check examples/decoupling/design.json --require-physical-ports
pcb-dev build examples/decoupling/design.json \
  --bindings examples/decoupling/bindings.json --hierarchical \
  --require-physical-ports --output build/decoupling-draft
```

J1 représente une source idéale déclarée et J2 un adaptateur de charge de test,
**pas un MCU qualifié**. Les seuils de cette fixture sont des hypothèses de test.
Le symbole C et l'empreinte de condensateur sont de vraies sources KiCad épinglées,
avec provenance et licences ; aucune référence commerciale n'est qualifiée.

## Mesurer le layout

L'API `check_layout(design, board, bindings)` exige un PCB et des bibliothèques
épinglées qui décrivent le design courant. Elle lit les pads électriques réels,
contrôle leur correspondance et leur géométrie avec les sources, puis transforme
leurs coordonnées par le placement et la rotation du footprint. Les trous NPTH
ne constituent pas des pads électriques. Une position de composant seule ne
remplace pas une position de pad.

La métrique est le **maximum**, sur tous les pads de la broche cible, de la
distance au pad d'alimentation du condensateur le plus proche. Un pad proche ne
masque donc pas un autre pad de la même broche éloigné. Pour la fixture nominale,
les centres des pads d'alimentation sont à `(32,54 ; 20)` et `(32,54 ; 21)` mm,
après rotations de 90° et 270° : la distance
est 1 mm pour une limite déclarée de 1,2 mm.

`native-check` et le manager incluent la preuve `decoupling-layout` lorsque le
modèle déclare des règles. Son hash de PCB est lié à celui de la DRC native. Une
bibliothèque absente, un PCB incohérent, une face non supportée ou une géométrie
manquante restent bloquants ; il n'y a pas de remplacement par des pads génériques
ou une distance entre centres de composants.

Les rapports séparent capacité, tension, connectivité, source et proximité. Déplacer
le condensateur sur le PCB peut violer G11 même si le preflight C6 reste sain.
Les exigences existantes, leurs sources, les ratings et la capacité déclarée ne
peuvent pas être réécrits par Qwen pour faire disparaître une erreur.

## Ce que cette mesure ne prouve pas

La distance entre centres de pads n'est ni une longueur de piste ni une mesure
d'inductance de boucle. Le retour de masse, le cuivre, les vias, l'ESR/ESL, le DC-bias,
les tolérances et le comportement transitoire restent à qualifier. Le premier mode
utilise des footprints sur la face supérieure ; la face inférieure reste à ajouter.
Un résultat C6/G11 réussi ne vaut ni choix de composant réel, ni validation de
fabrication. Les DRC non routées restent bloquantes.
