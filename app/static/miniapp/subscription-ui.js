const appRoot = document.querySelector('#app');

function paymentHref(root) {
  const link = root.querySelector('a.retry[href]');
  return link?.href || 'https://game.hidenow.su';
}

function polishChannelGate(root) {
  const cards = [...root.querySelectorAll('main.page .settings-card')];
  const section = cards.find(card => card.querySelector('h3')?.textContent?.includes('Подпишитесь на канал'));
  if (!section || section.dataset.channelGatePolished === '1') return;

  const channelLink = section.querySelector('a[href]')?.href || '#';
  section.dataset.channelGatePolished = '1';
  section.className = 'channel-gate-card';
  section.innerHTML = `
    <div class="channel-gate-card__glow" aria-hidden="true"></div>
    <div class="channel-gate-card__content">
      <div class="channel-gate-title">
        <span class="channel-gate-title__icon" aria-hidden="true">📣</span>
        <h2>Подпишитесь на канал</h2>
      </div>
      <p class="channel-gate-lead">Для работы с Phantom необходимо быть подписанным на наш информационный канал.</p>
      <div class="channel-gate-divider"></div>
      <div class="channel-gate-points">
        <div class="channel-gate-point"><span>▣</span><p>Здесь мы публикуем новости, обновления и важные уведомления.</p></div>
        <div class="channel-gate-point"><span>♢</span><p>Подписка обязательна для использования бота и Mini App.</p></div>
        <div class="channel-gate-point"><span>▣</span><p>После подписки нажмите «Проверить подписку».</p></div>
      </div>
      <div class="channel-gate-actions">
        <a class="channel-gate-button channel-gate-button--primary" href="${channelLink}" target="_blank" rel="noopener noreferrer">
          <span aria-hidden="true">➤</span> Открыть канал
        </a>
        <button class="channel-gate-button channel-gate-button--secondary" type="button" data-retry>
          <span aria-hidden="true">✓</span> Проверить подписку
        </button>
      </div>
    </div>
  `;

  const main = section.closest('main.page');
  if (main && !main.querySelector('.channel-gate-note')) {
    main.insertAdjacentHTML('beforeend', '<p class="channel-gate-note">♢ Мы не рассылаем спам и не передаём данные третьим лицам.</p>');
  }
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
        <p class="subscription-terms">Продолжая оплату, вы соглашаетесь с <a href="https://mooncloud.ltd/spy/terms.html#free" target="_blank" rel="noopener noreferrer">условиями пользования</a>.</p>
        <a class="retry subscription-pay" href="${href}" target="_blank" rel="noopener noreferrer">Продолжить оплату</a>
      </div>
    </section>
  `;
}

function polish() {
  if (!appRoot) return;
  polishChannelGate(appRoot);
  polishProfile(appRoot);
  polishSubscription(appRoot);
}

const observer = new MutationObserver(polish);
if (appRoot) observer.observe(appRoot, { childList: true, subtree: true });
polish();
