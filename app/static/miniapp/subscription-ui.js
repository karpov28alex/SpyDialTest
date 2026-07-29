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
    <section class="settings-card subscription-card">
      <div class="subscription-card__glow" aria-hidden="true"></div>
      <div class="subscription-card__content">
        <h3>VIP-подписка</h3>
        <p class="subscription-lead"><b>👉 Стоимость пробной VIP подписки — 20 ₽ за 1 день VIP статуса.</b></p>
        <p class="subscription-copy">Выбирая любой из тарифов, вы соглашаетесь с автоматической пролонгацией 125 ₽ каждые 7 дней по истечению оплаченного периода. Возможно частичное списание 70 ₽ за 3 дня VIP статуса.</p>
        <p class="subscription-terms">Продолжая оплату, вы соглашаетесь с <a href="https://spy.mooncloud.ltd/terms" target="_blank" rel="noopener noreferrer">условиями пользования</a>.</p>
        <a class="retry subscription-pay" href="${href}" target="_blank" rel="noopener noreferrer">Продолжить оплату</a>
      </div>
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
