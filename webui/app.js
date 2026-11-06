'use strict';

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  mode: 'simulate',
  health: null,
  currentJob: null,
  pollTimer: null,
  lastResult: null,
  uploadInfo: {},
};

// ---------------------------------------------------------------- 工具
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || JSON.stringify(j); } catch (_) {}
    throw new Error(detail);
  }
  const ct = res.headers.get('content-type') || '';
  return ct.includes('application/json') ? res.json() : res.text();
}

function num(sel, fallback = 0) {
  const v = parseFloat($(sel).value);
  return Number.isFinite(v) ? v : fallback;
}

function fmt(v, digits = 1, unit = '') {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${Number(v).toFixed(digits)}${unit}`;
}

function classify(v, good, mid, higherBetter = true) {
  if (v === null || v === undefined) return '';
  if (higherBetter) {
    if (v >= good) return 'val-good';
    if (v >= mid) return 'val-mid';
    return 'val-bad';
  }
  return '';
}

// ---------------------------------------------------------------- 健康状态
async function loadHealth() {
  try {
    const h = await api('/api/health');
    state.health = h;
    const badge = $('#apm-badge');
    if (h.apm.available) {
      badge.className = 'badge badge-ok';
      badge.textContent = 'APM 可用';
    } else {
      badge.className = 'badge badge-bad';
      badge.textContent = 'APM 不可用';
    }
    badge.title = h.apm.message;
    const p = h.apm.platform;
    $('#platform').textContent = `${p.system} / ${p.arch} · ${p.lib_exists ? 'lib OK' : 'lib missing'}`;
    if (!h.apm.available) addLog(`APM 库不可用: ${h.apm.message}`);
  } catch (e) {
    const badge = $('#apm-badge');
    badge.className = 'badge badge-bad';
    badge.textContent = '服务未连接';
  }
}

async function loadDevices() {
  try {
    const showAll = $('#dev-show-all')?.checked ? '?all=true' : '';
    const d = await api('/api/devices' + showAll);
    const inSel = $('#dev-input');
    const outSel = $('#dev-output');
    const keepIn = inSel.value;
    const keepOut = outSel.value;
    inSel.innerHTML = '<option value="">默认输入</option>';
    outSel.innerHTML = '<option value="">默认输出</option>';
    if (!d.available) {
      inSel.innerHTML = '<option value="">不可用</option>';
      outSel.innerHTML = '<option value="">不可用</option>';
      return;
    }
    const label = (dev) => (showAll
      ? `${dev.index}: ${dev.name} [${dev.hostapi_name}]`
      : `${dev.name}`);
    d.inputs.forEach((dev) => {
      const opt = document.createElement('option');
      opt.value = dev.index;
      opt.textContent = label(dev);
      inSel.appendChild(opt);
    });
    d.outputs.forEach((dev) => {
      const opt = document.createElement('option');
      opt.value = dev.index;
      opt.textContent = label(dev);
      outSel.appendChild(opt);
    });
    // 刷新后尽量保留用户原来的选择
    if (keepIn && [...inSel.options].some((o) => o.value === keepIn)) inSel.value = keepIn;
    else if (d.default_input !== null && d.default_input !== undefined && d.default_input >= 0) inSel.value = d.default_input;
    if (keepOut && [...outSel.options].some((o) => o.value === keepOut)) outSel.value = keepOut;
    else if (d.default_output !== null && d.default_output !== undefined && d.default_output >= 0) outSel.value = d.default_output;

    const hint = $('#dev-list-hint');
    if (hint) {
      hint.textContent = showAll
        ? `已显示后端全部原始条目（共 ${d.raw_count} 个），同一声卡会在多个后端重复出现。`
        : `已合并重复设备：默认优先 WASAPI，同一声卡的其他后端条目已隐藏。`;
    }
  } catch (e) {
    addLog(`设备列表获取失败: ${e.message}`);
  }
}

async function loadUploads() {
  try {
    const data = await api('/api/uploads');
    state.uploadInfo = {};
    data.items.forEach((it) => { state.uploadInfo[it.file_id] = it; });
    $$('.upload-select').forEach((sel) => {
      const current = sel.value;
      sel.innerHTML = '<option value="">— 选择已上传音频 —</option>';
      data.items.forEach((it) => {
        const opt = document.createElement('option');
        opt.value = it.file_id;
        const ch = it.channels ? `${it.channels}ch · ` : '';
        opt.textContent = `${it.file_id} (${ch}${fmt(it.duration_s, 2)}s)`;
        sel.appendChild(opt);
      });
      if (current) sel.value = current;
    });
    syncFileOneHint();
  } catch (_) {}
}

function syncFileSourceMode() {
  const mode = document.querySelector('input[name="file-src"]:checked')?.value || 'stereo';
  $('#file-single-wrap').classList.toggle('hidden', mode !== 'stereo');
  $('#file-two-wrap').classList.toggle('hidden', mode !== 'two');
}

function syncFileOneHint() {
  const hint = $('#file-one-hint');
  if (!hint) return;
  const id = $('#file-one').value;
  const info = id ? state.uploadInfo?.[id] : null;
  if (!info) {
    hint.textContent = '选择文件后这里会显示检测到的声道数。';
    hint.classList.remove('warn-text');
    return;
  }
  if (info.channels == null) {
    hint.textContent = '无法探测该文件的声道数（可能 ffmpeg 未安装或格式不受支持）。';
    hint.classList.add('warn-text');
  } else if (info.channels < 2) {
    hint.textContent = `该文件只有 ${info.channels} 个声道，不能用作单文件模式——请改用「两个文件」。`;
    hint.classList.add('warn-text');
  } else {
    hint.textContent = `检测到 ${info.channels} 个声道 · ${fmt(info.duration_s, 2)}s · 请确认 mic/ref 声道选择正确。`;
    hint.classList.remove('warn-text');
  }
}

async function uploadFile(file, targetSel) {
  const fd = new FormData();
  fd.append('file', file);
  addLog(`上传 ${file.name} …`);
  const info = await api('/api/upload', { method: 'POST', body: fd });
  if (info.decode_error) addLog(`解码告警: ${info.decode_error}`);
  else {
    const ch = info.channels ? ` · ${info.channels}ch` : '';
    addLog(`已上传 ${info.name} · ${fmt(info.duration_s, 2)}s${ch} · RMS ${fmt(info.rms_dbfs, 1)}dBFS`);
    if (info.channels === 1 && targetSel === 'file-one') {
      addLog('提示：单声道文件不能用作「一个双声道文件」模式，请改用「两个文件」。');
    }
  }
  await loadUploads();
  const sel = $(targetSel.startsWith('#') ? targetSel : `#${targetSel}`);
  if (sel && info.file_id) sel.value = info.file_id;
  syncFileOneHint();
}

// ---------------------------------------------------------------- 参数收集
function collectAec() {
  return {
    rate: parseInt($('#sim-rate').value, 10),
    preset: $('#aec-preset').value,
    echo: $('#aec-echo').checked,
    mobile_mode: $('#aec-mobile').checked,
    enforce_highpass: true,
    highpass: $('#aec-hp').checked,
    ns_enabled: $('#aec-ns').checked,
    ns_level: parseInt($('#aec-ns-level').value, 10),
    agc1: $('#aec-agc1').checked,
    agc1_mode: parseInt($('#aec-agc1-mode').value, 10),
    agc2: $('#aec-agc2').checked,
    transient: $('#aec-transient').checked,
    delay_ms: num('#aec-delay', 60),
    delay_auto: $('#aec-delay-auto').checked,
    delay_search_max: num('#aec-delay-max', 500),
  };
}

function collectOptions() {
  return {
    save_aec_in: $('#opt-save-in').checked,
    suppress_mic_during_far: $('#opt-mute-during-far').checked,
  };
}

function collectSimulate(prefix, rateSel) {
  const srcRef = $(`#${prefix}-ref-source`).value;
  const srcMic = $(`#${prefix}-mic-source`) ? $(`#${prefix}-mic-source`).value : 'speech';
  return {
    rate: parseInt($(rateSel).value, 10),
    duration_s: num(`#${prefix}-duration`, 6),
    ref_source: srcRef,
    ref_file: srcRef === 'file' ? ($(`#${prefix}-ref-file`)?.value || '') : '',
    mic_source: srcMic,
    mic_file: srcMic === 'file' ? ($(`#${prefix}-mic-file`)?.value || '') : '',
    echo_delay_ms: num(`#${prefix}-echo-delay`, 50),
    baseline_ms: num(`#${prefix}-baseline`, 300),
  };
}

// ---------------------------------------------------------------- 启动任务
async function startJob() {
  const aec = collectAec();
  const options = collectOptions();
  let path = '';
  let body = {};

  if (state.mode === 'simulate') {
    const refSrc = $('#sim-ref-source').value;
    const micSrc = $('#sim-mic-source').value;
    if (refSrc === 'file' && !$('#sim-ref-file').value) return alert('请选择或上传参考信号音频');
    if (micSrc === 'file' && !$('#sim-mic-file').value) return alert('请选择或上传近端人声音频');
    path = '/api/simulate';
    body = {
      aec,
      options,
      simulate: {
        rate: aec.rate,
        duration_s: num('#sim-duration', 6),
        ref_source: refSrc,
        ref_file: $('#sim-ref-file').value,
        mic_source: micSrc,
        mic_file: $('#sim-mic-file').value,
        speech_scale: num('#sim-speech', 0.35),
        noise_level: num('#sim-noise', 0.004),
        echo_delay_ms: num('#sim-echo-delay', 50),
        echo_gain: num('#sim-echo-gain', 0.6),
        echo_gain2: num('#sim-echo-gain2', 0.15),
        room_rt60: num('#sim-rt60', 0.15),
        nonlinear: num('#sim-nonlin', 0.15),
        baseline_ms: num('#sim-baseline', 300),
      },
    };
  } else if (state.mode === 'files') {
    const srcMode = document.querySelector('input[name="file-src"]:checked')?.value || 'stereo';
    if (srcMode === 'stereo') {
      const one = $('#file-one').value;
      if (!one) return alert('请选择一个双声道音频文件');
      const micCh = parseInt($('#file-mic-ch').value, 10);
      const refCh = parseInt($('#file-ref-ch').value, 10);
      if (micCh === refCh) return alert('mic 与 ref 不能选同一个声道');
      const f = state.uploadInfo?.[one];
      if (f && f.channels === 1) {
        return alert('该文件是单声道，无法同时作为 mic 与 ref。请用双声道文件，或改用「两个文件」模式。');
      }
      if (f && f.channels && (micCh >= f.channels || refCh >= f.channels)) {
        return alert(`该文件只有 ${f.channels} 个声道，所选声道超出范围。`);
      }
      path = '/api/files';
      body = { file: one, mic_channel: micCh, ref_channel: refCh, aec, options };
    } else {
      const mic = $('#file-mic').value;
      const ref = $('#file-ref').value;
      if (!mic || !ref) return alert('请选择 mic 与 ref 两个音频文件');
      path = '/api/files';
      body = { mic_file: mic, ref_file: ref, aec, options };
    }
  } else if (state.mode === 'device') {
    if (!$('#dev-play').checked) {
      return alert('为避免突然出声，请先勾选「允许播放参考音」。若只想录一段回采，请用音频文件模式。');
    }
    const refSrc = $('#dev-ref-source').value;
    if (refSrc === 'file' && !$('#dev-ref-file').value) return alert('请选择或上传参考音');
    path = '/api/device';
    const inDev = $('#dev-input').value;
    const outDev = $('#dev-output').value;
    body = {
      aec: { ...aec, rate: parseInt($('#dev-rate').value, 10) },
      options,
      input_device: inDev === '' ? null : parseInt(inDev, 10),
      output_device: outDev === '' ? null : parseInt(outDev, 10),
      device_rate: parseInt($('#dev-rate').value, 10),
      play: true,
      duration_s: num('#dev-duration', 8),
      play_gain: num('#dev-gain', 0.7),
      simulate: {
        rate: parseInt($('#dev-rate').value, 10),
        ref_source: refSrc,
        ref_file: $('#dev-ref-file').value,
        duration_s: num('#dev-duration', 8),
      },
    };
  } else {
    const presets = $$('#cmp-presets input:checked').map((el) => el.value);
    if (!presets.length) return alert('至少选择一个配置');
    const delays = $('#cmp-delays').value.split(',').map((s) => parseInt(s.trim(), 10)).filter(Number.isFinite);
    path = '/api/compare';
    body = {
      aec,
      options,
      presets,
      delays,
      simulate: {
        rate: aec.rate,
        duration_s: num('#cmp-duration', 6),
        ref_source: 'speech',
        mic_source: 'speech',
        echo_delay_ms: num('#cmp-echo-delay', 50),
      },
    };
  }

  $('#btn-run').disabled = true;
  $('#btn-cancel').disabled = false;
  $('#job-bar').classList.remove('err');
  setStage('提交任务…', 0);
  clearLog();
  try {
    const job = await api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    state.currentJob = job.id;
    $('#job-title').textContent = job.title;
    pollJob();
  } catch (e) {
    addLog(`提交失败: ${e.message}`);
    setStage('提交失败', 0, true);
    $('#btn-run').disabled = false;
    $('#btn-cancel').disabled = true;
  }
}

// ---------------------------------------------------------------- 轮询
function pollJob() {
  clearTimeout(state.pollTimer);
  const id = state.currentJob;
  if (!id) return;
  api(`/api/jobs/${id}`).then((job) => {
    setStage(`${job.stage}${job.status === 'running' ? ` · ${new Date().toLocaleTimeString()}` : ''}`, job.progress);
    renderLog(job.logs);
    if (job.status === 'running' || job.status === 'pending') {
      state.pollTimer = setTimeout(pollJob, 400);
      return;
    }
    $('#btn-run').disabled = false;
    $('#btn-cancel').disabled = true;
    if (job.status === 'done') {
      $('#job-bar').classList.remove('err');
      setStage('完成', 1);
      renderResult(job.result);
      loadResults();
    } else if (job.status === 'cancelled') {
      setStage('已取消', job.progress);
    } else {
      $('#job-bar').classList.add('err');
      setStage(`失败: ${job.error}`, job.progress, true);
    }
  }).catch((e) => {
    addLog(`状态查询失败: ${e.message}`);
    state.pollTimer = setTimeout(pollJob, 1200);
  });
}

function setStage(text, progress, isErr) {
  $('#job-stage').textContent = text;
  const pct = Math.max(0, Math.min(1, progress || 0)) * 100;
  $('#job-bar').style.width = `${pct}%`;
  if (isErr) $('#job-bar').classList.add('err');
}

function clearLog() { $('#job-log').textContent = ''; }
function renderLog(lines) {
  const box = $('#job-log');
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
  box.textContent = (lines || []).join('\n');
  if (atBottom) box.scrollTop = box.scrollHeight;
}
function addLog(msg) {
  const box = $('#job-log');
  box.textContent += `${box.textContent ? '\n' : ''}[${new Date().toLocaleTimeString()}] ${msg}`;
  box.scrollTop = box.scrollHeight;
}

// ---------------------------------------------------------------- 结果渲染
const ART_LABEL = {
  'metrics.json': 'JSON',
};

function renderResult(result) {
  if (!result) return;
  state.lastResult = result;
  const m = result.metrics?.summary;
  const box = $('#metrics');
  box.innerHTML = '';

  if (m) {
    const items = [
      { k: 'ERLE（纯回声段）', v: fmt(m.erle_db, 1), u: 'dB', cls: classify(m.erle_db, 20, 10) },
      { k: 'ERLE（双讲段）', v: fmt(m.double_talk_erle_db, 1), u: 'dB', cls: classify(m.double_talk_erle_db, 8, 3) },
      { k: '近端保留', v: fmt(m.near_retention_db, 1), u: 'dB', cls: classify(m.near_retention_db, -6, -12) },
      { k: '回声耦合损耗', v: fmt(m.echo_return_loss_db, 1), u: 'dB', cls: '' },
      { k: '估计/设定延迟', v: fmt(m.delay_ms, 0), u: 'ms', cls: '' },
      { k: '输出峰值', v: fmt(m.out_peak_dbfs, 1), u: 'dBFS', cls: classify(m.out_peak_dbfs, -1, -3, false) },
    ];
    items.forEach((it) => {
      const el = document.createElement('div');
      el.className = 'metric';
      el.innerHTML = `<div class="k">${it.k}</div><div class="v ${it.cls}">${it.v}<span class="u">${it.u}</span></div>`;
      box.appendChild(el);
    });
  }

  const renderWarnings = (list) => {
    const old = document.getElementById('run-warnings');
    if (old) old.remove();
    if (!list || !list.length) return;
    const w = document.createElement('div');
    w.id = 'run-warnings';
    w.className = 'notice';
    w.innerHTML = list.map((s) => `⚠ ${s}`).join('<br>');
    $('#metrics').before(w);
  };
  const warnings = result.metrics?.aec_stats?.warnings || [];
  renderWarnings(warnings);

  // 对比表
  const cmpWrap = $('#compare-table');
  if (result.compare) {
    const { rows, truth_delay_ms } = result.compare;
    const best = Math.max(...rows.map((r) => r.erle_db ?? -999));
    let html = `<table><caption>真实回声延迟估计 ${fmt(truth_delay_ms, 0)} ms</caption>
      <thead><tr><th>配置</th><th>设定延迟</th><th>ERLE</th><th>双讲 ERLE</th><th>近端保留</th><th>实时因子</th></tr></thead><tbody>`;
    rows.forEach((r) => {
      const warn = (r.warnings || []).length
        ? `<span title="${r.warnings.join('; ')}" style="color:#e2b04a"> ⚠</span>` : '';
      html += `<tr><td>${r.preset_name}${warn}</td><td>${r.delay_ms} ms</td>
        <td class="${r.erle_db === best ? 'best' : ''}">${fmt(r.erle_db, 1)}</td>
        <td>${fmt(r.double_talk_erle_db, 1)}</td><td>${fmt(r.near_retention_db, 1)}</td>
        <td>${fmt(r.rtf, 4)}</td></tr>`;
    });
    html += '</tbody></table>';
    cmpWrap.innerHTML = html;
    cmpWrap.classList.remove('hidden');
  } else {
    cmpWrap.classList.add('hidden');
  }

  // 分段表
  const oldSeg = $('#seg-table');
  if (oldSeg) oldSeg.remove();
  if (result.metrics?.segments) {
    const t = document.createElement('div');
    t.id = 'seg-table';
    t.className = 'seg-table compare-wrap';
    let html = `<table><caption>分段指标</caption><thead><tr>
      <th>段落</th><th>时间 (s)</th><th>mic</th><th>out</th><th>ERLE</th><th>近端保留</th></tr></thead><tbody>`;
    Object.entries(result.metrics.segments).forEach(([name, s]) => {
      html += `<tr><td>${name}</td><td>${fmt(s.start_s, 2)}–${fmt(s.end_s, 2)}</td>
        <td>${fmt(s.mic_dbfs, 1)}</td><td>${fmt(s.out_dbfs, 1)}</td>
        <td>${fmt(s.erle_db, 1)}</td><td>${fmt(s.near_retention_db, 1)}</td></tr>`;
    });
    html += '</tbody></table>';
    t.innerHTML = html;
    $('#artifacts').before(t);
  }

  // 产物
  const art = $('#artifacts');
  art.innerHTML = '';
  (result.artifacts || []).forEach((a) => {
    const el = document.createElement('div');
    el.className = 'artifact';
    const isAudio = a.kind === 'wav';
    el.innerHTML = `
      <div style="min-width:0">
        <div class="label">${a.label}</div>
        <div class="desc">${a.name} · ${(a.size / 1024).toFixed(0)} KB · ${a.desc}</div>
      </div>
      ${isAudio ? `<audio controls preload="none" src="${a.url}"></audio>` : ''}
      <div class="links">
        <a href="${a.url}" download>下载</a>
        ${a.kind === 'png' ? `<a href="#" onclick="showPlot('${a.url}');return false">查看</a>` : ''}
        ${a.kind === 'json' ? `<a href="#" onclick="showJson('${a.url}');return false">查看</a>` : ''}
      </div>`;
    art.appendChild(el);

    if (a.kind === 'png') {
      $('#plot-img').src = a.url;
      $('#plot-wrap').classList.remove('hidden');
    } else if (a.name === 'plot.png') {
      $('#plot-img').src = a.url;
      $('#plot-wrap').classList.remove('hidden');
    }
  });
  if (!(result.artifacts || []).some((a) => a.name === 'plot.png')) {
    $('#plot-wrap').classList.add('hidden');
  }
}

window.showPlot = function (url) {
  window.open(url, '_blank');
};
window.showJson = async function (url) {
  try {
    const text = await api(url);
    const w = window.open('', '_blank');
    if (w) {
      const esc = text.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' })[c]);
      w.document.write(`<pre style="font:12px/1.5 monospace;white-space:pre-wrap">${esc}</pre>`);
    }
  } catch (e) {
    alert(`读取失败: ${e.message}`);
  }
};

async function loadResults() {
  const wrap = $('#results-history');
  if (!wrap) return;
  try {
    const data = await api('/api/results');
    const list = data.items || [];
    if (!list.length) {
      wrap.innerHTML = '<p class="hint">还没有历史结果，先跑一次测试。</p>';
      return;
    }
    wrap.innerHTML = '';
    list.slice(0, 20).forEach((it) => {
      const s = it.summary || {};
      const row = document.createElement('div');
      row.className = 'history-item';
      const when = new Date(it.mtime * 1000).toLocaleString();
      const detail = s.type === 'compare'
        ? '参数对比'
        : `ERLE ${fmt(s.erle_db, 1)} dB · 双讲 ${fmt(s.double_talk_erle_db, 1)} dB`;
      row.innerHTML = `
        <div style="min-width:0">
          <div class="label mono">${it.dir}</div>
          <div class="desc">${when} · ${detail}</div>
        </div>
        <div class="links"><a href="#" data-dir="${it.dir}">载入</a></div>`;
      row.querySelector('a').addEventListener('click', (ev) => {
        ev.preventDefault();
        openHistory(it.dir);
      });
      wrap.appendChild(row);
    });
  } catch (e) {
    wrap.innerHTML = `<p class="hint">历史读取失败: ${e.message}</p>`;
  }
}

async function openHistory(dir) {
  try {
    const metrics = await api(`/files/${dir}/metrics.json`);
    const result = {
      dir,
      artifacts: [],
      metrics,
      segments: metrics.segments,
    };
    // 推断产物清单（无法列目录时按常见文件名拼）
    const names = [
      ['aec_in.wav', 'AEC 输入（L=mic / R=ref）', '进入 AEC 前的两路原始信号'],
      ['mic.wav', 'mic 原始', 'AEC 前的麦克风采集'],
      ['ref.wav', 'ref 参考', 'AEC 前的扬声器参考'],
      ['aec_out.wav', 'AEC 输出', '回声消除后的信号'],
      ['near_truth.wav', '近端真值（仿真）', '用于计算近端保留率'],
      ['plot.png', '波形与 ERLE 图', '波形 + ERLE 曲线', 'png'],
      ['metrics.json', '指标 JSON', '分段指标', 'json'],
    ];
    for (const [name, label, desc, kind] of names) {
      result.artifacts.push({
        name, label, desc,
        kind: kind || (name.endsWith('.png') ? 'png' : name.endsWith('.json') ? 'json' : 'wav'),
        url: `/files/${dir}/${name}`,
        size: 0,
      });
    }
    renderResult(result);
    $('#job-title').textContent = `历史结果 ${dir}`;
    setStage('已载入历史结果', 1);
  } catch (e) {
    alert(`载入失败: ${e.message}`);
  }
}

// ---------------------------------------------------------------- 事件绑定
function bindTabs() {
  $$('#mode-tabs .tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      $$('#mode-tabs .tab').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.mode = btn.dataset.mode;
      $$('.pane').forEach((p) => p.classList.toggle('hidden', p.dataset.pane !== state.mode));
    });
  });
}

function bindPreset() {
  const apply = () => {
    const p = $('#aec-preset').value;
    const on = (sel, v) => { $(sel).checked = v; };
    if (p === 'off') { on('#aec-echo', false); on('#aec-mobile', false); on('#aec-hp', false); on('#aec-ns', false); on('#aec-agc1', false); on('#aec-agc2', false); }
    else if (p === 'aec_only') { on('#aec-echo', true); on('#aec-mobile', false); on('#aec-hp', false); on('#aec-ns', false); on('#aec-agc1', false); on('#aec-agc2', false); }
    else if (p === 'voice_assistant') { on('#aec-echo', true); on('#aec-mobile', false); on('#aec-hp', true); on('#aec-ns', true); on('#aec-agc1', false); on('#aec-agc2', false); $('#aec-ns-level').value = '1'; }
    else if (p === 'desktop') { on('#aec-echo', true); on('#aec-mobile', false); on('#aec-hp', true); on('#aec-ns', true); on('#aec-agc1', true); on('#aec-agc2', false); $('#aec-ns-level').value = '2'; }
    else if (p === 'mobile') { on('#aec-echo', true); on('#aec-mobile', true); on('#aec-hp', true); on('#aec-ns', true); on('#aec-agc1', false); on('#aec-agc2', false); $('#aec-ns-level').value = '2'; }
  };
  $('#aec-preset').addEventListener('change', apply);
  $$('.switches input, #aec-ns-level, #aec-agc1-mode').forEach((el) => {
    el.addEventListener('change', () => {
      if ($('#aec-preset').value !== 'custom') $('#aec-preset').value = 'custom';
    });
  });
}

function bindFileVis() {
  const sync = () => {
    const ref = $('#sim-ref-source').value;
    $('#sim-ref-file-wrap').classList.toggle('hidden', ref !== 'file');
    const mic = $('#sim-mic-source').value;
    $('#sim-mic-file-wrap').classList.toggle('hidden', mic !== 'file');
    $('#dev-ref-file-wrap').classList.toggle('hidden', $('#dev-ref-source').value !== 'file');
  };
  ['#sim-ref-source', '#sim-mic-source', '#dev-ref-source'].forEach((sel) => {
    $(sel).addEventListener('change', sync);
  });
  sync();
}

function bindFileSource() {
  $$('input[name="file-src"]').forEach((el) => {
    el.addEventListener('change', syncFileSourceMode);
  });
  $('#file-one')?.addEventListener('change', syncFileOneHint);
  $('#file-mic-ch')?.addEventListener('change', () => {
    // 避免误选同一路：选中后把另一侧自动挪开
    if ($('#file-mic-ch').value === $('#file-ref-ch').value) {
      const other = ['0', '1', '2', '3'].find((v) => v !== $('#file-mic-ch').value);
      if (other !== undefined) $('#file-ref-ch').value = other;
    }
  });
  $('#file-ref-ch')?.addEventListener('change', () => {
    if ($('#file-mic-ch').value === $('#file-ref-ch').value) {
      const other = ['0', '1', '2', '3'].find((v) => v !== $('#file-ref-ch').value);
      if (other !== undefined) $('#file-mic-ch').value = other;
    }
  });
  syncFileSourceMode();
}

function bindUploads() {
  $$('.upload-input').forEach((input) => {
    input.addEventListener('change', async (ev) => {
      const file = ev.target.files?.[0];
      if (!file) return;
      try { await uploadFile(file, input.dataset.target); } catch (e) { alert(`上传失败: ${e.message}`); }
      ev.target.value = '';
    });
  });
}

function bindActions() {
  $('#btn-run').addEventListener('click', startJob);
  $('#btn-cancel').addEventListener('click', async () => {
    if (!state.currentJob) return;
    await api(`/api/jobs/${state.currentJob}/cancel`, { method: 'POST' });
    addLog('已请求取消');
  });
  $('#btn-refresh').addEventListener('click', () => { loadHealth(); loadDevices(); loadUploads(); });
  $('#btn-devices').addEventListener('click', loadDevices);
  $('#dev-show-all')?.addEventListener('change', loadDevices);
  $('#btn-results-refresh').addEventListener('click', loadResults);
  $('#btn-download-aec-in')?.addEventListener('click', () => {
    const url = state.lastResult?.dir ? `/files/${state.lastResult.dir}/aec_in.wav` : null;
    if (url) window.open(url, '_blank');
    else alert('还没有可用的结果，先跑一次仿真');
  });
  $('#dev-play').addEventListener('change', (ev) => {
    if (ev.target.checked) {
      if (!confirm('即将打开扬声器播放参考音并开启麦克风录音，确认继续？')) ev.target.checked = false;
    }
  });
}

// 进入设备模式时惰性拉取设备
function bindDeviceLazy() {
  const tab = document.querySelector('#mode-tabs .tab[data-mode="device"]');
  tab?.addEventListener('click', loadDevices, { once: true });
}

window.addEventListener('DOMContentLoaded', async () => {
  bindTabs();
  bindPreset();
  bindFileVis();
  bindFileSource();
  bindUploads();
  bindActions();
  bindDeviceLazy();
  // 默认预设初始化
  $('#aec-preset').dispatchEvent(new Event('change'));
  await loadHealth();
  await loadUploads();
  await loadResults();
});
