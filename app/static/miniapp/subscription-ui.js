const appRoot = document.querySelector('#app');

function paymentHref(root) {
  const link = root.querySelector('a.retry[href]');
  return link?.href || 'https://game.hidenow.su';
}

function polishProfile(root) {
  const button = root.querySelector('[data-go="subscription"]');
  if (!button) return;
  const section = button.closest('.settings-card');
  if (!section || section.dataset.subscriptionPolished === '1') return;
  section.dataset.subscriptionPolished = '1';
  section.innerHTML = `
    <h3>VIP-подписка</h3>
    <p class="muted">Откройте условия подписки и продолжите тестовую оплату.</p>
    <button class="retry" data-go="subscription">Приобрести подписку</button>
  `;
}

function polishSubscription(root) {
  const title = root.querySelector('.topbar .title');
  if (title?.textContent?.trim() !== 'Подписка') return;
  const page = root.querySelector('main.page');
  if (!page || page.dataset.subscriptionPolished === '1') return;

  const href = paymentHref(page);
  page.dataset.subscriptionPolished = '1';
  page.innerHTML = `
    <section class="settings-card">
      <h3>VIP-подписка</h3>
      <p><b>👉 Стоимость пробной VIP подписки — 1 ₽ за 1 день VIP статуса.</b></p>
      <p>Выбирая любой из тарифов, вы соглашаетесь с автоматической пролонгацией 299 ₽ каждые 3 дня по истечению оплаченного периода. Возможно частичное списание 99 ₽ за 1 день VIP статуса.</p>
      <p>Продолжая оплату, вы соглашаетесь с <a href="https://sms.evocloud.su/terms" target="_blank" rel="noopener noreferrer">условиями пользования</a>.</p>
      <a class="retry" href="${href}" target="_blank" rel="noopener noreferrer">Продолжить оплату</a>
    </section>
  `;
}

function polish() {
  if (!appRoot) return;
  polishProfile(appRoot);
  polishSubscription(appRoot);
}

const observer = new MutationObserver(polish);
if (appRoot) observer.observe(appRoot, { childList: true, subtree: true });
polish();
