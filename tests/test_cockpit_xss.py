"""The cockpit page must render untrusted mission data inert and never touch web storage / prompt()."""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from agentic_os.mission.api import _COCKPIT_HTML

XSS = '"><img src=x onerror=alert(1)>'
QUOTE = "x' onmouseover='alert(1)"


def test_static_page_has_no_inline_handlers_or_token_storage():
    h = _COCKPIT_HTML
    for bad in ("onclick", "sessionStorage", "localStorage", "prompt(", "indexedDB"):
        assert bad not in h, bad
    assert "&quot;" in h and "&#39;" in h        # esc() covers attribute contexts


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_injected_payloads_render_inert(tmp_path):
    script = _COCKPIT_HTML.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    (tmp_path / "page.js").write_text(script)
    harness = textwrap.dedent(f"""
      const vm=require('vm'), fs=require('fs');
      const XSS={json.dumps(XSS)}, Q={json.dumps(QUOTE)};
      const out=[];
      const el=()=>{{const e={{_h:'',value:'',hidden:false,style:{{}},classList:{{add(){{}},remove(){{}}}},
        addEventListener(){{}},scrollIntoView(){{}},options:[],appendChild(){{}},
        set innerHTML(v){{this._h=v;out.push(v);}},get innerHTML(){{return this._h;}},
        set textContent(v){{out.push(String(v));}},get textContent(){{return '';}}}};return e;}};
      const els={{}};
      const doc={{getElementById:i=>els[i]||(els[i]=el()),querySelectorAll:()=>[],addEventListener(){{}}}};
      const node={{id:Q,capability:XSS,operator:XSS,status:XSS,depends_on:[],produces:XSS,result:{{summary:XSS,k:XSS}}}};
      const routes={{
        '/inbox':{{inbox:[{{mission_id:Q,node_id:Q,kind:'approval',goal:XSS,prompt:XSS,evidence:[XSS]}}]}},
        '/missions':{{missions:[{{id:Q,goal:XSS,template:XSS,state:XSS}}]}},
        ['/missions/'+Q]:{{id:Q,goal:XSS,state:XSS,template:XSS,outcome:XSS,
          active_plan:{{revision:XSS,reason:XSS,graph:{{nodes:[node]}},projection:{{expected_approvals:XSS}}}}}},
        ['/missions/'+Q+'/events']:{{timeline:[{{type:XSS,payload:{{node_id:Q,capability:XSS}}}}]}},
        ['/missions/'+Q+'/explain']:{{world_state:{{[XSS]:{{value:{{a:XSS}},confidence:.5,state:XSS}}}},steps:[{{type:XSS,payload:{{}}}}],
          decisions:{{[Q]:{{capability:XSS,reason:XSS,latency_ms:XSS,alternatives:[]}}}}}},
      }};
      const ctx={{window:{{}},document:doc,location:{{search:''}},URLSearchParams,JSON,Math,Number,String,Object,Array,Promise,
        setInterval(){{}},fetch:async u=>({{status:200,json:async()=>routes[u]||{{}}}}),
        get sessionStorage(){{throw new Error('storage used')}},get localStorage(){{throw new Error('storage used')}},
        prompt(){{throw new Error('prompt used')}},console}};
      vm.createContext(ctx);
      vm.runInContext(fs.readFileSync(process.argv[2],'utf8')+'\\n;globalThis.__f={{openMission,openNode,renderDetail}};',ctx);
      (async()=>{{
        await new Promise(r=>setTimeout(r,20));
        for (const t of ['graph','world','timeline','explain']) {{
          await ctx.__f.openMission(Q); vm.runInContext('TAB='+JSON.stringify(t),ctx); ctx.__f.renderDetail();
        }}
        ctx.__f.openNode(Q);
        console.log(JSON.stringify(out));
      }})().catch(e=>{{console.error(e);process.exit(1);}});
    """)
    (tmp_path / "h.js").write_text(harness)
    r = subprocess.run(["node", str(tmp_path / "h.js"), str(tmp_path / "page.js")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    html = json.loads(r.stdout.strip().splitlines()[-1])
    blob = "\n".join(html)
    assert len(blob) > 2000 and "&lt;img src=x onerror=alert(1)&gt;" in blob
    assert "<img" not in blob and "<script" not in blob
    assert XSS not in blob and QUOTE not in blob
    assert 'data-args="[&quot;x&#39; onmouseover=&#39;alert(1)&quot;' in blob or "&#39; onmouseover=&#39;" in blob
