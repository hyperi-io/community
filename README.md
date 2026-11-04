# HyperI community

Ask questions, share what you've built and suggest ideas for Data Fusion Engine and Edge Stream Hub in [Discussions](https://github.com/hyperi-io/community/discussions).

## Voting on ideas

Give an idea a thumbs-up to vote for it. Once a day a bot posts each idea's community score as a comment.

A thumbs-up counts when the account is at least 90 days old and has 5 or more GitHub contributions in the past year. That keeps throwaway accounts from stacking the count. Votes from HyperI customers count three times. Plain upvotes show in the score too but aren't weighted.

An idea that scores 10 or more gets a tracked issue labelled `community-idea`, which puts it on our planning board. The issue links back to the idea and its score updates daily.

A thumbs-up on an open issue in any public hyperi-io repo counts the same way. The daily run writes the score to the issue's Community score field, which we sort by when we set priorities.

The thresholds live in `scoring.toml`. The scorer is `scripts/score_ideas.py`, run by `.github/workflows/score-ideas.yml`.
