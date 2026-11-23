const orderForm = document.querySelector('#order-form');
if (orderForm) {
  const info = orderForm.querySelector('#order-info');
  const exportLink = orderForm.querySelector('#order-export');
  const eventId = orderForm.dataset.event;
  let currentEventId = null;

  async function publish(rerun) {
    info.textContent = '正在核对削减与总表核减…';
    try {
      const url = rerun ? `/api/response-orders/${currentEventId || eventId}/rerun` : '/api/response-orders';
      const response = await fetch(url, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: rerun ? undefined : JSON.stringify({sample: orderForm.dataset.sample}),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message);
      currentEventId = payload.manifest.event_id;
      const wording = {created: '已发布', reused: '同事件重跑 · 复用原单', conflict: '输入已变 · 拒绝重复发放'}[payload.publish_status];
      info.textContent = `${wording} · ${payload.order_id}`;
      exportLink.href = `/api/response-orders/${currentEventId}/export`;
      exportLink.hidden = false;
    } catch (error) {
      info.textContent = error.message || '补偿单发布失败。';
    }
  }

  orderForm.addEventListener('submit', event => { event.preventDefault(); publish(false); });
  orderForm.querySelector('#order-rerun').addEventListener('click', () => publish(true));
}
