import { AnsiUp } from 'ansi_up'
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { useEffect, useId, useRef, useState } from 'react'
import type { Output } from '../api'

const ansi = new AnsiUp()
ansi.use_classes = true

const join = (v: string | string[] | undefined) => (Array.isArray(v) ? v.join('') : v ?? '')

export function renderMarkdown(src: string) {
  return DOMPurify.sanitize(marked.parse(src, { async: false }) as string)
}

function Ansi({ text, className }: { text: string; className?: string }) {
  return <pre className={className} dangerouslySetInnerHTML={{ __html: ansi.ansi_to_html(text) }} />
}

/** Rich HTML (pandas tables, plotly…) in a sandboxed iframe that reports its height. */
function HtmlFrame({ html }: { html: string }) {
  const id = useId()
  const ref = useRef<HTMLIFrameElement>(null)
  const [height, setHeight] = useState(40)

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (e.source === ref.current?.contentWindow && e.data?.frame === id) {
        setHeight(Math.min(Math.max(e.data.height, 20), 2000))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [id])

  const doc = `<!doctype html><html><head><meta charset="utf-8"><style>
    body{margin:0;font:13px system-ui,sans-serif;color:#1f2328;background:#fff;overflow:auto}
    table{border-collapse:collapse}td,th{border:1px solid #d0d7de;padding:3px 8px;text-align:right}
    th{background:#f6f8fa}tr:nth-child(even) td{background:#fafbfc}
  </style></head><body>${html}<script>
    const post=()=>parent.postMessage({frame:${JSON.stringify(id)},height:document.documentElement.scrollHeight},'*');
    new ResizeObserver(post).observe(document.documentElement);addEventListener('load',post);post();
  </script></body></html>`
  return (
    <iframe
      ref={ref}
      className="html-output"
      sandbox="allow-scripts"
      srcDoc={doc}
      style={{ height }}
      title="sortie HTML"
    />
  )
}

function RichData({ data }: { data: Record<string, string | string[]> }) {
  if (data['image/png']) return <img className="img-output" src={`data:image/png;base64,${join(data['image/png'])}`} />
  if (data['image/jpeg']) return <img className="img-output" src={`data:image/jpeg;base64,${join(data['image/jpeg'])}`} />
  if (data['image/svg+xml'])
    return <img className="img-output" src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(join(data['image/svg+xml']))}`} />
  if (data['text/html']) return <HtmlFrame html={join(data['text/html'])} />
  if (data['text/markdown'])
    return <div className="markdown" dangerouslySetInnerHTML={{ __html: renderMarkdown(join(data['text/markdown'])) }} />
  if (data['application/json']) return <pre>{JSON.stringify(data['application/json'], null, 2)}</pre>
  if (data['text/plain']) return <Ansi text={join(data['text/plain'])} />
  return <pre className="muted">[{Object.keys(data).join(', ')}]</pre>
}

export function OutputView({ output }: { output: Output }) {
  switch (output.output_type) {
    case 'stream':
      return <Ansi text={join(output.text)} className={output.name === 'stderr' ? 'stderr' : undefined} />
    case 'error':
      return (
        <div className="error-output">
          {output.traceback?.length ? (
            <Ansi text={output.traceback.join('\n')} />
          ) : (
            <pre>
              {output.ename}: {output.evalue}
            </pre>
          )}
        </div>
      )
    default:
      return <RichData data={output.data ?? {}} />
  }
}
