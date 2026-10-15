# Tutoriel : architecture USB-C, alimentation, MCU et LED

Ce parcours fonctionne hors ligne après installation de Python et du paquet.
Il produit un aperçu de modèle ; aucun composant du démonstrateur n'est qualifié
pour être commandé ou assemblé.

## 1. Vérifier l'installation

Depuis la racine du dépôt, avec l'environnement virtuel activé :

```sh
python -m pip install -e .
pcb-dev --version
pcb-dev doctor
```

`doctor` indique si KiCad est installé. Son absence n'empêche pas les commandes
`check`, `graph`, `inspect`, `build`, `verify`, `archive` et `tolerance`.
`doctor --require-kicad` renvoie un statut d'échec si les contrôles natifs ne
peuvent pas être exécutés.

## 2. Comprendre les blocs

`examples/usb_mcu_led.json` décrit cinq blocs :

```text
board
├── usb       VBUS 4,75–5,25 V, 500 mA disponibles
├── power     entrée VBUS, sortie 3,25–3,35 V, 300 mA disponibles
├── mcu       charge de 80 mA, sortie GPIO
└── led       charge déclarée de 10 mA, entrée de commande
```

L'alimentation dépend de l'USB ; le MCU et la LED dépendent de l'alimentation.
La LED dépend aussi du MCU. Les connexions utilisent des chemins absolus
comme `board.power.vout` et des noms de nets explicites comme `3V3`.

```sh
pcb-dev graph examples/usb_mcu_led.json --impact board.power
pcb-dev inspect examples/usb_mcu_led.json
```

L'impact comprend `board.mcu` et `board.led`. Le rapport des paramètres contient
notamment `dissipation_w = 0.153` pour le régulateur et une estimation de
résistance série de `325 Ω` pour la LED. La valeur physique déclarée de `330 Ω`
reste explicite ; une équation ne la remplace pas automatiquement.

## 3. Faire passer les contrôles du modèle

```sh
pcb-dev check examples/usb_mcu_led.json
pcb-dev check examples/usb_mcu_led.json --json --output build/preflight.json
```

Le rail 3V3 alimente 90 mA de charges déclarées pour une capacité de 300 mA.
Le contrôle compare l'enveloppe complète de la tension source à la plage
acceptée par chaque charge. Un avertissement rappelle que les règles actuelles
du fabricant n'ont pas été vérifiées. Les avertissements ne sont pas bloquants.

Les deux fichiers suivants doivent échouer avec le code de sortie `1` :

```sh
pcb-dev check examples/invalid_voltage.json
pcb-dev check examples/invalid_power_budget.json
```

Le premier augmente la tension maximale du régulateur au-delà de ce qu'accepte
le MCU. Le second réduit sa capacité à 50 mA. Ces exemples montrent des erreurs
de modèle, pas des résultats SPICE ou des mesures de carte.

## 4. Construire et revoir un aperçu

```sh
pcb-dev build examples/usb_mcu_led.json --output build/tutorial
pcb-dev verify build/tutorial
```

| Fichier | Utilisation |
| --- | --- |
| `index.html` | Revue autonome des blocs et de leur placement déclaré |
| `design.json` | Modèle normalisé avec valeurs par défaut |
| `preflight.json`, `preflight.md` | Constats et preuve du design analysé |
| `design.md` | Hiérarchie, interfaces d'alimentation et rapport |
| `parameters.json`, `dependency-order.json` | Paramètres résolus et ordre de construction |
| `bom.csv`, `bom-jlcpcb.csv` | Nomenclatures indicatives ; les MPN/LCSC ne sont pas inventés |
| `placement-draft.csv` | Centres déclarés, rotations et face ; pas de correction fabricant implicite |
| `kicad/` | Aperçus génériques de schéma et PCB, netlist, projet et règles |
| `build-lock.json` | Version de l'outil, versions de blocs et état des contrôles natifs |
| `manifest.json` | Hash et taille de tous les fichiers générés |

La visionneuse n'envoie rien sur le réseau. Ouvrir `index.html` localement puis
choisir un bloc dans la colonne de gauche. Le PCB de démonstration n'est pas
routé. Le contour et les centres ne prouvent pas la compatibilité mécanique.

```sh
pcb-dev archive build/tutorial --output build/tutorial-preview.zip
```

L'archive est reproductible pour les mêmes données et la même version du
générateur. Son manifeste conserve `kind: preview`. Toute modification d'un
fichier après construction fait échouer `verify` et bloque l'archivage.

## 5. Protéger un bloc et explorer les tolérances

```sh
pcb-dev freeze examples/usb_mcu_led.json board.mcu --output build/frozen.json
pcb-dev check build/frozen.json
pcb-dev tolerance examples/usb_mcu_led.json board.led \
  --relative led_forward_v=0.05 --relative led_current_a=0.01 \
  --samples 100 --seed 42
```

Le gel inclut les paramètres hérités, les connexions et les dépendances du
bloc. Une modification qui change ce contexte produit un constat bloquant.
Les tolérances utilisent des échantillons uniformes indépendants et les coins
des intervalles : ce sont des estimations d'équations, pas des bornes certifiées
ou une simulation du fonctionnement réel de la LED.

## 6. Passer aux outils réels

Utiliser `native-check` sur des sources KiCad réelles pour conserver les preuves
d'ERC, DRC et de parité avec le modèle. Les pads génériques de cet exemple doivent
d'abord être remplacés. Voir [backends](backends.md), [blocs et paquets](blocks.md)
et [harnais d'agent](agent.md).

Le prochain jalon physique exige des composants réels, leurs pinouts et empreintes
qualifiés, le routage et la revue des exports fabricant. Le tutoriel ne les remplace pas.
