#!/bin/sh
set -eu

if [ -d .git ]; then
  printf '%s\n' 'Git repository already initialized.'
else
  git init
  git branch -M main
  printf '%s\n' 'Initialized Git repository on branch main.'
fi

git add .
printf '%s\n' 'Files staged. Review with: git status && git diff --cached'
printf '%s\n' 'Then create the first commit with: git commit -m "Hidden Conviction v0.14.0"'
