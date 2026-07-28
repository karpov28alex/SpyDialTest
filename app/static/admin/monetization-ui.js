function replaceText(root, from, to) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    if (node.nodeValue?.includes(from)) node.nodeValue = node.nodeValue.replaceAll(from, to);
  }
}

function polishAdminMonetization() {
  replaceText(document.body, 'Доступ и тарифы', 'Доступ и подписка');
  replaceText(document.body, 'Показывать тарифы', 'Показывать кнопку подписки');
  replaceText(document.body, 'Показывать блок тарифов и кнопку оплаты', 'Показывать в профиле Mini App кнопку «Приобрести подписку»');
  replaceText(document.body, 'Trial, рефералы, ручная выдача и тестовая оплата', 'Trial, рефералы, ручная выдача и отображение подписки');
}

const observer = new MutationObserver(polishAdminMonetization);
observer.observe(document.documentElement, { childList: true, subtree: true });
polishAdminMonetization();
