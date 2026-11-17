# Message formatting reference

A message is a JSON object. `text` is required; everything else is optional.

```json
{
  "chat_id": "<chat_id from list>",
  "title": "One bold line",
  "text": "Markdown subset, see below",
  "table": [["Header A", "Header B"], ["cell", "cell"]],
  "table_header": true,
  "format": "markdown",
  "occurrence": "2026-W47"
}
```

| Field | Rules |
|---|---|
| `chat_id` | Exact id from `python teammatic.py list` |
| `text` | 1 to 8000 characters, LF line endings |
| `title` | Single line, up to 200 characters, rendered bold above the text |
| `table` | Up to 20 rows by 7 columns; every row the same width; cells are single-line plain text up to 300 characters |
| `table_header` | `true` (default) makes the first row bold |
| `format` | `markdown` (default) or `plain` to send `text` exactly as written |
| `occurrence` | Free label, up to 64 characters, that marks an intended repeat of the same content |

Order in the message: title, text, table.

## Markdown subset

### Inline

| Syntax | Result | Notes |
|---|---|---|
| `**text**` | bold | |
| `*text*`, `_text_` | italic | `2*3*4` and `snake_case_name` stay literal |
| `***text***` | bold italic | marks can be nested |
| `~~text~~` | strikethrough | |
| `` `text` `` | inline code | nothing inside is interpreted |
| `[label](https://example.com)` | link | `https://`, `http://` and `mailto:` only; anything else stays literal text |
| `\*` | a literal `*` | backslash also escapes `` \ ` _ ~ [ ] ( ) # > . - `` |

A single line break inside a paragraph is kept as a line break. An empty line starts a new paragraph.

### Blocks

| Syntax | Result |
|---|---|
| `# Title`, `## Title`, `### Title` | heading, levels 1 to 3 |
| `- item` or `* item` on consecutive lines | bullet list |
| `1. item` or `1) item` on consecutive lines | numbered list |
| `> text` on consecutive lines | quote |
| a line of three backticks, lines of code, a line of three backticks | code block, content taken literally |

Inline marks work inside headings, list items and quotes.

### Example

```json
{
  "messages": [
    {
      "chat_id": "<chat_id>",
      "title": "🚀 Release 2.4 is live",
      "text": "Hi team,\n\n## What changed\n\n- faster search\n- export to CSV\n\n1. Update the app\n2. Sign in again\n\n> Rollback stays possible until Friday.\n\nNotes: [changelog](https://example.com/changelog)",
      "table": [["Area", "Owner"], ["Search", "Anna"]]
    }
  ]
}
```

A longer one is in [`examples/message.json`](../examples/message.json).

## What is not supported, and why

| Not supported | Reason |
|---|---|
| Raw HTML, CSS, scripts | The tool composes only markup it generated itself, so a message file can never inject anything |
| Text and highlight colors | Poor contrast on the purple bubble of your own messages; a draft containing a color style is refused |
| Underline | No Markdown syntax for it; use bold or italic |
| Mentions (`@name`) | Need internal Teams identifiers; text `@name` is sent as plain text and notifies nobody |
| Attachments, images, GIFs, stickers | Not part of the message box text |
| Nested lists, task lists | Kept out to stay predictable across Teams versions |
| Tables inside lists, merged cells | Same |

Unsupported Markdown is not an error: it is sent as the literal characters you wrote. `plan` shows the
visible text of every message in `preview`, so you see what the recipient will read.

## How formatting is applied and checked

1. `plan` parses `text` and builds fixed HTML from it. Your characters are escaped; only the tags in the
   tables above are ever produced.
2. A message with no formatting is typed as plain text. A message with formatting enters the message box
   through the editor's own paste handling, which keeps only what your Teams version supports.
3. Before the Send click the draft is read back. Its visible text must equal the approved text, and every
   format you asked for must be present. If not, nothing is sent, the draft stays in the message box for you
   to look at, and the job stays `pending` with the reason, for example
   `FORMAT_NOT_APPLIED_LEAVE_FOR_INSPECTION heading`.
4. After sending, the stored message is compared again. A difference in formatting at this point is reported
   as a `warning` on a `sent` job.

So a format that your Teams version does not accept costs you one refused send, never a wrong message.

## Writing messages that read well in Teams

- Lead with the point. Put the one thing the reader must do in the first line or in `title`.
- Prefer a short list over a long paragraph, and a table for anything with two or more attributes per row.
- Use bold for the two or three words that matter, not for whole sentences.
- Emoji work as visual anchors at the start of a title or a list item. One per line is plenty.
- Keep tables narrow: three or four columns read well in a chat window; seven is the hard limit.
- For personalized batches, read every expanded message in the `plan` output before approving.
