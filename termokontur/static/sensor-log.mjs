export function createSerialLog(output, endButton, maxLength) {
  const text = output.ownerDocument.createTextNode("");
  output.replaceChildren(text);
  const scrollToEnd = () => { output.scrollTop = output.scrollHeight; };
  endButton.addEventListener("click", scrollToEnd);

  return {
    append(line) {
      if (!line) return;
      const top = output.scrollTop;
      const follow = output.scrollHeight - output.clientHeight - top <= 2;
      text.appendData(line + "\n");
      const excess = text.length - maxLength;
      const heightBeforeTrim = !follow && excess > 0 ? output.scrollHeight : 0;
      if (excess > 0) text.deleteData(0, excess);

      if (follow) scrollToEnd();
      else output.scrollTop = top - (excess > 0 ? heightBeforeTrim - output.scrollHeight : 0);
      endButton.disabled = false;
    },
    clear() {
      text.data = "";
      output.scrollTop = 0;
      endButton.disabled = true;
    },
  };
}
