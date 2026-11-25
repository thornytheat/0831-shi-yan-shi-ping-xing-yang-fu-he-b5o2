import {element} from './client.js';

function block(target,className,content) {
  const node=element('div',content,{class:className});
  target.append(node);
  return node;
}

export function visualize(report,selected) {
  const target=document.querySelector('#visual');
  target.replaceChildren();
  const measurement=report.cases?.find(row=>row.case===selected)?.measure;
  if (!measurement) {block(target,'chart-note','选择有计算值的分组');return;}
  const e=measurement.evidence;

  for(const [sample,info] of Object.entries(e.pairs)) {
    const line=block(target,'sample-rule'); block(line,'sample-id',`样 ${sample}`);
    block(line,'sample-method',`${info.mode} · ${info.value ?? '—'}`);
    block(line,info.pass?'sample-pass':'sample-fail',info.pass?'通过':'复核');
  }
}
