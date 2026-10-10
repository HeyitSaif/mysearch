# Gold sets for `mse eval`

A gold set is a JSON file of questions written by hand, each with the document it should find:

```json
{
  "_about": "What this set is, in a sentence or two. Read by people; the code ignores it.",
  "queries": [
    {"q": "invoice from Acme for March 2025", "expect": ["Invoices/2025-03_invoice_acme"], "collection": "work"}
  ]
}
```

* `q`: the question, as you would type it in the search box.
* `expect`: fragments of the path of the right document, case-insensitive. A result counts when its path contains
  one of them.
* `collection` (optional): the folder or group to search. Without it the search uses the folders given with `-c`,
  or All.
* A question whose expected document is not indexed is skipped, and the skip count is reported.

## Where the sets are read from

1. every `*.json` in your data folder, `~/Library/Application Support/MySearch/eval/` (your own sets; they stay on
   your Mac, outside the repository), then
2. every `*.json` in this folder, except `gold_example.json`.

A set is named by its file name without `.json`. When the same name exists in both places, the copy in the data folder
is used and the other is skipped, so a set is never counted twice.

The questions of every set are held out: the re-ranker learning never trains on them.

## Run one

    mse eval eval/gold_example.json -c work --configs hybrid,keyword,semantic,rerank -v

Use `eval/gold_example.json` as a template. Its three questions are invented, and their documents do not exist.
