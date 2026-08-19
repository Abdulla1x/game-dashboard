**What this changes**

**Why**

**Checklist**
- [ ] `py -m unittest discover tests` passes
- [ ] Verified on Windows (not only WSL)
- [ ] No new dependencies — standard library only
- [ ] If rendering changed: the grid is still reused, not rebuilt, and any new rendered
      field is in `signature()`
- [ ] If a new HTTP route or field was added: it goes through the request guard and
      validates anything that reaches the filesystem
