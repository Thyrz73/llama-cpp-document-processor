# Correction UTF-8 des analyses V2

## Cause reproduite

`ModelClient.stream_lines()` utilisait
`response.iter_lines(chunk_size=64, decode_unicode=True)`. Requests attribue
`ISO-8859-1` à une réponse `Content-Type: text/event-stream` sans charset.
Le flux SSE est pourtant en UTF-8. Avant même `json.loads`, les octets de
`L’assemblée générale` devenaient `Lâ\x80\x99assemblÃ©e gÃ©nÃ©rale`.
Le problème est reproduit localement avec un véritable `requests.Response`,
sans Qwen ni llama.cpp. Il suffit que les événements contiennent les caractères
Unicode directement, plutôt que leurs échappements JSON `\uXXXX`.

Cette cause explique exactement les symptômes signalés. Les en-têtes et
fichiers du serveur de production n'ont pas été inspectés : ce diagnostic
repose sur le code et une reproduction du défaut de décodage.

## Diagnostic du chemin de texte

| Étape                             | Constat et vérification                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1. PyMuPDF / pypdf / Tesseract    | Les deux lecteurs PDF renvoient des chaînes Unicode. Leurs sorties françaises, anglaises et multilingues simulées sont intactes. Tesseract renvoie des octets UTF-8 ; le décodage devient strict et échoue avec `invalid_ocr_encoding` si ces octets sont invalides. Une mauvaise table de caractères du PDF ou une erreur de reconnaissance reste possible à la source. |
| 2. Normalisation / découpage      | Opérations sur `str`, aucune conversion Latin-1. Suppression des caractères nuls et nettoyage des espaces, comme auparavant. Le découpage réel conserve le texte Unicode et les références de pages dans les tests du pipeline.                                                                                                                                          |
| 3. Prompts Qwen                   | Les consignes et extraits restent des `str`. Le prompt administrateur est lu en UTF-8, puis conservé dans le snapshot. Le choix `fr` / `en` modifie uniquement les consignes et les métadonnées du rapport.                                                                                                                                                              |
| 4. JSON envoyé / HTTP             | `json=payload` dans Requests sérialise correctement le JSON et ajoute `application/json`. Les échappements ASCII éventuels sont du JSON valide et ne perdent aucun caractère. Le test inspecte le corps de la véritable requête préparée, après désérialisation.                                                                                                         |
| 5. SSE reçu                       | **Premier point de corruption identifié** : choix implicite de Latin-1 par Requests. Le flux reste maintenant en octets jusqu'à reconstitution de chaque ligne, puis est décodé une fois en UTF-8 strict. Les en-têtes charset ne pilotent plus ce décodage.                                                                                                             |
| 6. Raisonnement / réponse         | `ThinkingSplitter` travaille sur Unicode. Tests des deux canaux séparés et des balises `<think>` fragmentées : contenu et raisonnement conservent le texte exact. Avant correction, ils recevaient déjà du texte corrompu.                                                                                                                                               |
| 7. Analyses Markdown              | `atomic_text` écrit en UTF-8 ; `Artifacts.read` relit en UTF-8. Ces fonctions conservaient fidèlement la corruption reçue du SSE. Tests des octets enregistrés pour chaque note.                                                                                                                                                                                         |
| 8. Fusions hiérarchiques          | Les notes sont jointes en Unicode, les prompts passent par JSON, les réponses par le même client SSE corrigé. Tests d'une fusion réelle de quatre notes et du passage d'une note isolée vers la synthèse.                                                                                                                                                                |
| 9. Rapport final                  | Le corps du modèle et les métadonnées localisées sont écrits en UTF-8 par `atomic_text`. Tests distincts du corps conservé dans les artefacts et du rapport publié.                                                                                                                                                                                                      |
| 10. SQLite / API / téléchargement | SQLite reçoit des `str` par paramètres, sans conversion manuelle. Les snapshots et questions ressortent intacts. Flask sérialise les réponses JSON sans perte ; les échappements Unicode sont valides. `send_file` conserve les octets et la route fixe explicitement `text/markdown; charset=utf-8`. Waitress n'effectue pas de conversion métier du contenu.           |
| 11. Frontend                      | `<meta charset="utf-8">` existe déjà. `response.json()` et `textContent` conservent Unicode ; le téléchargement utilise `response.blob()` sans réencodage. Le scénario frontend vérifie aussi l'intégrité après changement de langue et de thème.                                                                                                                        |

## Fichiers et fonctions concernés

- `document_processor/model_client.py` : `json_request`, `stream_lines`, `generate`.
  Les réponses JSON sont décodées explicitement en UTF-8. Les lignes SSE sont
  reconstituées en octets puis décodées strictement ; `invalid_stream_encoding`
  interdit de terminer une génération contenant des octets invalides.
- `document_processor/pipeline.py` : `run.saved_step`. Une dépendance
  `text_transport_version: 1` empêche de réutiliser les sorties générées avant
  correction, même si leurs sommes de contrôle sont correctes.
- `app.py` : `read_system_prompt`, `ocr_page`, `report`. Gestion contrôlée d'un
  prompt invalide, décodage OCR strict et charset Markdown explicite.
- `scripts/benchmark.py` : `main`. Lecture des variantes et critères et écriture
  des résultats avec `encoding="utf-8"`.
- `scripts/check_jobs.py` : `inspect`. Lecture explicite du manifeste en UTF-8.
- `tests/test_encoding.py` : tests des frontières du pipeline et des reprises.
- `tests/test_polling.js` : vérification du texte français et multilingue affiché.

Aucun paramètre d'inférence, budget de tokens ou profil de performance n'est
modifié. Le protocole SSE conserve ses limites, délais et mécanismes d'annulation.
Aucune conversion heuristique n'est appliquée aux chaînes Unicode valides.

## Vérification reproductible

```bash
python -m unittest discover -s tests -p test_encoding.py -v
python -m unittest discover -s tests -v
python -m compileall -q app.py document_processor scripts tests
node tests/test_polling.js
node --check static/app.js
docker compose config --quiet
```

Le texte français de référence est comparé exactement, ainsi que l'anglais et
un mélange contenant ligatures, guillemets, unités, symboles scientifiques,
chinois, japonais, arabe, emoji et accent combinant. Les lettres valides `Ã`,
`Â` et `â` sont également conservées. Les événements JSON SSE contiennent du
vrai Unicode (`ensure_ascii=False`) et traversent des fragments réseau de
1, 2, 3, 7 et 64 octets. Les tests couvrent un charset absent, UTF-8 et un
charset trompeur Windows-1252. Ils utilisent l'itération réelle de Requests,
ce qui évite qu'un faux `iter_lines` masque le défaut.

Le pipeline simulé exerce le véritable découpage, les requêtes JSON préparées,
la réception SSE, les notes, une fusion, le rapport, les fichiers, SQLite,
l'aperçu JSON et le téléchargement Flask. Il échoue volontairement avant le
rapport final, puis reprend les notes vérifiées. Un autre scénario simule les
dépendances d'avant correction et vérifie leur recalcul et l'archivage du
rapport publié précédent.

Les tests ne mesurent pas la qualité des réponses réelles de Qwen et ne
garantissent pas la qualité de l'extraction d'un PDF mal formé ou de l'OCR.
La validation visuelle finale doit utiliser un nouveau petit PDF français dans
l'instance mise à jour et son Markdown téléchargé.

Résultats dans l'environnement local de correction : 9 tests d'encodage
réussis ; sur les 66 tests Python, 64 réussissent et les 2 tests d'OCR réel
échouent uniquement parce que l'exécutable Tesseract est absent. Les tests
d'OCR simulé réussissent. Les 20 scénarios frontend passent dans V8 avec une
simulation d'`AbortController` (Node absent). La compilation Python et
`git diff --check` réussissent. Docker est absent : la construction de l'image,
la validation Compose et les vérifications en production restent à effectuer.

## Mise à jour manuelle du conteneur

Intégrer le correctif dans la version transférée au serveur. `git archive`
n'inclut que les fichiers commités : vérifier que les nouveaux tests et ce
document sont inclus avant de fabriquer l'archive.

1. Attendre la fin des analyses et arrêter uniquement Document Processor.
2. Sauvegarder `data/` à l'arrêt, y compris SQLite et ses fichiers WAL/SHM,
   ainsi que `.env` et `config/system_prompt.txt`.
3. Copier les fichiers applicatifs mis à jour en préservant ces données et
   configurations, puis reconstruire le conteneur depuis le répertoire du projet :

```bash
docker compose config --quiet
docker compose build --no-cache document-processor
docker compose up -d --no-deps --force-recreate document-processor
docker compose logs --tail=100 document-processor
```

Utiliser les mêmes options `-f` que l'installation si elle emploie un fichier
de surcharge réseau. Le service llama.cpp n'a pas besoin d'être redémarré.
Rafraîchir l'interface, lancer une nouvelle analyse avec la langue « Français »,
vérifier l'aperçu puis télécharger le Markdown. Contrôler accents, apostrophes,
`−2,5 °C` et `±0,2 %`. Garder la sauvegarde jusqu'à validation.

Aucun déploiement ni accès au serveur de production n'est effectué par ce
correctif. Le démarrage ne lance aucune migration de réparation des textes.
Un job terminé reste téléchargeable à l'identique. Une reprise d'un job échoué,
annulé ou interrompu recalcule ses anciennes sorties du modèle, conserve
l'extraction vérifiée et archive le précédent rapport publié avant remplacement.
Cette première reprise peut donc nécessiter davantage d'appels au modèle.
Pour un ancien job terminé, lancer une nouvelle analyse du PDF.

## Anciens rapports : réparation séparée et contrôlée

Un fichier peut être un UTF-8 parfaitement valide tout en contenant du
mojibake. Changer son charset HTTP ou le réenregistrer en UTF-8 ne répare pas
son contenu. Si tous les octets sont encore représentés par les caractères
Latin-1 correspondants, l'inversion stricte est réversible :
`fragment.encode("latin-1").decode("utf-8")`. Windows-1252 est une autre
hypothèse possible, à confirmer sur le fichier concerné. L'inversion ne doit
jamais être appliquée automatiquement à tout un rapport ou à tout `data/`.

Procédure proposée sur une copie hors production :

1. Conserver l'original binaire et sa somme SHA-256. Travailler sur un fichier
   isolé, jamais sur un artefact actif ni sur son manifeste.
2. Sélectionner un fragment **précisément identifié comme corrompu** et indiquer
   explicitement le texte attendu. Vérifier l'inversion stricte et son aller-retour.
3. Produire un fichier candidat distinct avec sauvegarde binaire de l'original.
   Ne modifier que l'occurrence sélectionnée. Refuser en cas d'ambiguïté.
4. Examiner le diff et le fichier dans un lecteur UTF-8 avant toute utilisation.
   Si des notes réutilisables sont corrompues, préférer une nouvelle analyse.

Exemple limité à une occurrence connue (adapter le chemin et le fragment après
inspection ; le fichier source n'est jamais réécrit) :

```python
from pathlib import Path
import hashlib

source = Path("/tmp/rapport-a-examiner.md")
original = source.read_bytes()
text = original.decode("utf-8")
expected = "L’assemblée générale"
damaged = expected.encode("utf-8").decode("latin-1")
if text.count(damaged) != 1:
    raise ValueError("Fragment absent ou ambigu : aucune correction produite.")
if damaged.encode("latin-1").decode("utf-8") != expected:
    raise ValueError("L'inversion ne correspond pas au texte attendu.")
candidate = text.replace(damaged, expected, 1).encode("utf-8")
with Path(str(source) + ".original").open("xb") as backup:
    backup.write(original)
with Path(str(source) + ".candidate.md").open("xb") as output:
    output.write(candidate)
print("SHA-256 original :", hashlib.sha256(original).hexdigest())
```

Cet exemple refuse un texte déjà correct sans fragment corrompu et préserve
tous les autres caractères. Le mode `xb` refuse d'écraser une sauvegarde ou
un candidat précédent. Ce n'est pas un détecteur automatique : un fragment
tel que `Ã©` peut être intentionnel et nécessite une décision humaine.
Si `�`, des caractères `?` de remplacement ou des octets supprimés ont déjà
remplacé l'information, aucune réparation exacte n'est garantie ; régénérer
depuis le PDF. Aucun remplacement global de `Ã`, `Â` ou `â` n'est proposé.
