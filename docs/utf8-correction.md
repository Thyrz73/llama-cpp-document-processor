# UTF-8 : comportement actuel et anciens rapports

## Cause et correction

Un ancien lecteur SSE utilisait `decode_unicode=True`. Pour un flux sans charset, Requests pouvait choisir Latin-1 et transformer les octets UTF-8 en texte comme `assemblÃ©e`. Le défaut a été reproduit avec une réponse HTTP simulée, sans appel à Qwen.

Le client actuel reconstitue les lignes SSE en octets, puis les décode une seule fois en UTF-8 strict. Les réponses JSON et la sortie OCR sont également décodées en UTF-8. Les fichiers texte et les snapshots sont lus et écrits en UTF-8 ; le téléchargement annonce `text/markdown; charset=utf-8`.

Un flux ou une sortie OCR contenant des octets invalides échoue avec `invalid_stream_encoding` ou `invalid_ocr_encoding`. Aucun remplacement automatique de caractères n'est appliqué.

Cela corrige le transport du texte. Une mauvaise extraction PDF, une erreur d'OCR ou un texte incorrect produit par le modèle restent possibles.

## Effet sur les analyses existantes

Les rapports terminés restent téléchargeables à l'identique. Le démarrage ne répare pas leurs textes.

Lors d'une reprise, la dépendance `text_transport_version` empêche de réutiliser les sorties du modèle antérieures à la correction. Les étapes d'extraction compatibles sont conservées. Un rapport publié remplacé est archivé dans `previous_reports/`.

La reprise garde les prompts et budgets enregistrés. Pour appliquer les prompts actuels ou refaire une analyse terminée, créer une nouvelle analyse du PDF.

## Vérifier

Depuis la racine du projet, avec les dépendances installées :

```bash
.venv/bin/python -m unittest discover -s tests -p test_encoding.py -v
node tests/test_polling.js
```

Les tests couvrent le français et d'autres écritures, les caractères UTF-8 coupés entre fragments réseau, les charsets trompeurs, les notes, les fusions, SQLite, l'aperçu et le téléchargement. Ils utilisent un modèle simulé et ne mesurent pas la qualité d'une réponse Qwen réelle.

Après [mise à jour du conteneur](migration-v2.md), lancer un petit PDF français et vérifier l'aperçu puis le Markdown téléchargé : accents, apostrophes, unités et symboles comme `−2,5 °C` et `±0,2 %`.

## Anciens fichiers corrompus

Un fichier UTF-8 valide peut déjà contenir du texte mal décodé. Modifier son charset ou le réenregistrer en UTF-8 ne restaure pas les caractères d'origine.

La solution la plus simple reste une nouvelle analyse depuis le PDF. Pour un rapport à conserver, travailler sur une copie distincte, garder l'original et examiner chaque correction. Ne pas modifier les artefacts actifs ou leurs manifestes.

L'inversion `fragment.encode("latin-1").decode("utf-8")` n'est valable que si le fragment a réellement suivi ce mauvais décodage. Elle ne doit pas être appliquée à tout un rapport : des caractères comme `Ã`, `Â` ou `â` peuvent être légitimes. Si des octets ont été perdus ou remplacés par `�`, une réparation exacte n'est pas garantie.
